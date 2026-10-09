$ErrorActionPreference = 'Stop'
$inkflowLauncherDirectory = 'D:\墨流\dev-launcher'
$inkflowLauncherExecutable = Join-Path $inkflowLauncherDirectory '墨流 InkFlow.exe'
$inkflowLauncherSource = Join-Path $PSScriptRoot 'InkFlowLauncher.cs'
$inkflowLauncherManifest = Join-Path $PSScriptRoot 'InkFlowLauncher.manifest'
$inkflowLauncherIcon = Join-Path (Split-Path -Parent $PSScriptRoot) 'desktop\resources\icon.ico'
$inkflowLauncherScript = Join-Path $PSScriptRoot 'start-desktop-hidden.ps1'
$inkflowLauncherStamp = Join-Path $inkflowLauncherDirectory 'build-hash.txt'
$inkflowLauncherFingerprint = (@($inkflowLauncherSource, $inkflowLauncherManifest, $inkflowLauncherIcon) |
    ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }) -join ':'
New-Item -ItemType Directory -Force -Path $inkflowLauncherDirectory | Out-Null
if (-not (Test-Path -LiteralPath $inkflowLauncherExecutable) -or
    -not (Test-Path -LiteralPath $inkflowLauncherStamp) -or
    (Get-Content -LiteralPath $inkflowLauncherStamp -Raw).Trim() -ne $inkflowLauncherFingerprint) {
    $inkflowCompiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $inkflowCompiler)) {
        $inkflowCompiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    }
    $inkflowCompiled = & $inkflowCompiler /nologo /target:winexe "/out:$inkflowLauncherExecutable" "/win32icon:$inkflowLauncherIcon" "/win32manifest:$inkflowLauncherManifest" /reference:System.Windows.Forms.dll $inkflowLauncherSource 2>&1
    if ($LASTEXITCODE -ne 0) { throw "墨流启动程序编译失败：$inkflowCompiled" }
    Set-Content -LiteralPath $inkflowLauncherStamp -Value $inkflowLauncherFingerprint -Encoding ASCII
}
[IO.File]::WriteAllText((Join-Path $inkflowLauncherDirectory 'launch-script.txt'), $inkflowLauncherScript, [Text.UTF8Encoding]::new($false))
$inkflowLauncherExecutable
