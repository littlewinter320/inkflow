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
$desktopVersion = (Get-Content -LiteralPath (Join-Path $desktopRoot 'package.json') -Raw | ConvertFrom-Json).version
$electronVersion = (Get-Content -LiteralPath (Join-Path $desktopRoot 'node_modules\electron\package.json') -Raw | ConvertFrom-Json).version
$iconHash = (Get-FileHash -LiteralPath $inkflowIcon -Algorithm SHA256).Hash.ToLowerInvariant().Substring(0,12)
$inkflowBrandedElectron = "D:\墨流\cache\desktop-runtime\$electronVersion-$desktopVersion-$iconHash\墨流 InkFlow.exe"
$inkflowNativeLauncher = & (Join-Path $PSScriptRoot 'build-development-launcher.ps1')
$inkflowShortcutPath = Join-Path $inkflowPrograms '墨流（本地开发）.lnk'
if ((Test-Path -LiteralPath $inkflowPrograms) -and (Test-Path -LiteralPath $inkflowIcon)) {
    $inkflowShortcutShell = New-Object -ComObject WScript.Shell
    $inkflowShortcut = $inkflowShortcutShell.CreateShortcut($inkflowShortcutPath)
    $inkflowShortcut.TargetPath = $inkflowNativeLauncher
    $inkflowShortcut.Arguments = ''
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
        if (-not [string]::Equals($existingShortcut.TargetPath, $inkflowNativeLauncher, [StringComparison]::OrdinalIgnoreCase)) {
            $inkflowShortcutBackup = Join-Path $logRoot 'shortcut-backup'
            New-Item -ItemType Directory -Force -Path $inkflowShortcutBackup | Out-Null
            $inkflowBackupPath = Join-Path $inkflowShortcutBackup ('墨流 InkFlow-before-local-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff') + '.lnk')
            Copy-Item -LiteralPath $inkflowDesktopShortcutPath -Destination $inkflowBackupPath
            Write-Output "桌面原快捷方式已备份：$inkflowBackupPath"
        }
    }
    $desktopShortcut = $inkflowShortcutShell.CreateShortcut($inkflowDesktopShortcutPath)
    $desktopShortcut.TargetPath = $inkflowNativeLauncher
    $desktopShortcut.Arguments = ''
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

# 同时识别旧入口与已更名的开发运行时；只接受本项目的精确路径，排除渲染子进程。
$inkflowRunning = Get-CimInstance Win32_Process -Filter "Name='electron.exe' OR Name='墨流 InkFlow.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        ([string]::Equals([string]$_.ExecutablePath, $inkflowElectron, [StringComparison]::OrdinalIgnoreCase) -or
         [string]::Equals([string]$_.ExecutablePath, $inkflowBrandedElectron, [StringComparison]::OrdinalIgnoreCase)) -and
        $_.CommandLine -notmatch '(?:^|\s)--type='
    } |
    Select-Object -First 1
if ($inkflowRunning) {
    Start-Process -FilePath $inkflowRunning.ExecutablePath -ArgumentList @("`"$desktopRoot`"") -WorkingDirectory $desktopRoot -WindowStyle Hidden
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

# 普通提示只说现象与下一步；作者主动选择后才打开本次日志，不把技术输出直接塞进弹窗。
function Show-InkFlowStartupFailure([string]$Message, [string]$LogPath) {
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
        if ($LogPath -and (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
            $choice = [InkFlowStartupNotice]::Show([IntPtr]::Zero,
                "$Message`n`n是否打开本次启动记录查看原因？", '墨流启动提示', 0x114)
            if ($choice -eq 6) {
                Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\notepad.exe') -ArgumentList @("`"$LogPath`"") -ErrorAction Stop
            }
        } else {
            [InkFlowStartupNotice]::Show([IntPtr]::Zero, $Message, '墨流启动提示', 0x10) | Out-Null
        }
    } catch {
        try { Add-Content -LiteralPath $stderr -Value "Unable to show startup notice: $($_.Exception.Message)" -Encoding UTF8 } catch { }
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
    $inkflowNodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    $inkflowNodeRecord = 'D:\墨流\dev-launcher\node-path.txt'
    $inkflowNodeCandidates = @(
        $(if ($inkflowNodeCommand) { $inkflowNodeCommand.Source }),
        $(if (Test-Path -LiteralPath $inkflowNodeRecord) { (Get-Content -LiteralPath $inkflowNodeRecord -Raw).Trim() }),
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe')
    )
    $inkflowNodeCandidates | Where-Object { $_ } | ForEach-Object {
        Add-Content -LiteralPath $stderr -Value ("Node candidate: {0}; exists={1}" -f $_, (Test-Path -LiteralPath $_ -PathType Leaf)) -Encoding UTF8
    }
    $node = $inkflowNodeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $node) { throw '本机未找到可用的 Node.js 运行时；请安装 Node.js 或在 node-path.txt 指定现有 node.exe 的完整路径。未自动下载或启动旧版。' }
    $inkflowNodeVersion = & $node --version 2>&1
    if ($LASTEXITCODE -ne 0 -or [string]$inkflowNodeVersion -notmatch '^v\d+\.\d+\.\d+') { throw '已找到的 Node.js 运行时无法正常启动；未改用其他墨流版本。' }
    [IO.File]::WriteAllText($inkflowNodeRecord, $node, [Text.UTF8Encoding]::new($false))
    $nodeRoot = Split-Path -Parent $node
    $devScript = Join-Path $desktopRoot 'scripts\dev.mjs'
    $desktopBin = Join-Path $desktopRoot 'node_modules\.bin'
    $env:Path = "$desktopBin;$nodeRoot;$windowsSystem32;$env:Path"
    $process = Start-Process `
        -FilePath $node `
        -ArgumentList @("`"$devScript`"") `
        -WorkingDirectory $desktopRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru `
        -ErrorAction Stop
} catch {
    $reason = $_.Exception.Message
    try { Add-Content -LiteralPath $stderr -Value $reason -Encoding UTF8 } catch { }
    Show-InkFlowStartupFailure -Message '墨流暂时无法启动，本地启动准备没有完成。请查看本次记录，确认需要处理的组件或启动条件，再重新打开。' -LogPath $stderr
    return
}

# Confirm a visible main window from this exact development launch, not an old log line
# or another Electron process. Vite, TypeScript and Electron remain hidden.
$startupTimeoutSeconds = 30
$deadline = (Get-Date).AddSeconds($startupTimeoutSeconds)
$ready = $false
$failure = ''
while ((Get-Date) -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) {
        $process.WaitForExit()
        Add-Content -LiteralPath $stderr -Value "Startup exited before window confirmation; exit_code=$($process.ExitCode)" -Encoding UTF8 -ErrorAction SilentlyContinue
        $failure = '工作台在窗口打开前就结束了。请先查看任务栏是否已有墨流窗口；没有的话，再启动一次。'
        break
    }

    $snapshot = @(Get-CimInstance Win32_Process -Filter "Name='electron.exe' OR Name='墨流 InkFlow.exe' OR Name='node.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue)
    if ($snapshot.Count -gt 0) {
        $descendants = Get-DescendantProcessIds -RootProcessId $process.Id -Processes $snapshot
        $mainWindows = @($snapshot | Where-Object {
            $descendants.ContainsKey([int]$_.ProcessId) -and
            ([string]::Equals([string]$_.ExecutablePath, $inkflowElectron, [StringComparison]::OrdinalIgnoreCase) -or
             [string]::Equals([string]$_.ExecutablePath, $inkflowBrandedElectron, [StringComparison]::OrdinalIgnoreCase)) -and
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
            Add-Content -LiteralPath $stderr -Value "Startup exited before window confirmation; exit_code=$($process.ExitCode)" -Encoding UTF8 -ErrorAction SilentlyContinue
            $failure = '工作台在窗口打开前就结束了。请先查看任务栏是否已有墨流窗口；没有的话，再启动一次。'
        } else {
            $failure = '墨流暂时还没有显示主窗口，可能仍在初始化。请先等片刻并查看任务栏，不要连续点击启动；仍打不开时查看本次记录。'
        }
    }
    $stdoutFile = Get-Item -LiteralPath $stdout -ErrorAction SilentlyContinue
    $detailLog = if ($stdoutFile -and $stdoutFile.Length -gt 0) { $stdout } else { $stderr }
    Show-InkFlowStartupFailure -Message $failure -LogPath $detailLog
    return
}

Write-Output "墨流开发版窗口已就绪（启动 PID $($process.Id)）。本次日志：$stdout；$stderr"
} finally {
    if ($startupLockHeld) { $startupMutex.ReleaseMutex() }
    $startupMutex.Dispose()
}
