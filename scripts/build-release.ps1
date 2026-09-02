$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $repositoryRoot '.venv\Scripts\pyinstaller.exe'
$engineOutput = Join-Path $repositoryRoot 'dist\engine'
$extensionBin = Join-Path $repositoryRoot 'vscode-extension\bin'
$extensionRelease = Join-Path $repositoryRoot 'vscode-extension\release'
$desktopRelease = Join-Path $repositoryRoot 'desktop\release'
$systemPowerShellDirectory = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0'
$system32Directory = Join-Path $env:SystemRoot 'System32'

# electron-builder invokes powershell.exe and cmd.exe by name. Some IDE-hosted
# Node runtimes omit the Windows system directories from PATH.
$env:PATH = "$systemPowerShellDirectory;$system32Directory;$env:PATH"

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Create the Python virtual environment in the repository root first.'
}

& $python -m pip install -e "${repositoryRoot}[dev,build]"
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed with exit code $LASTEXITCODE."
}

& $pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name inkflow-engine `
    --paths (Join-Path $repositoryRoot 'src') `
    --collect-submodules mcp.server `
    --collect-submodules mcp.shared `
    --collect-data mcp `
    --hidden-import mcp.types `
    --exclude-module mcp.cli `
    --distpath $engineOutput `
    --workpath (Join-Path $repositoryRoot 'build\pyinstaller') `
    --specpath (Join-Path $repositoryRoot 'build\pyinstaller') `
    (Join-Path $repositoryRoot 'scripts\inkflow_app_server_entry.py')
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath (Join-Path $engineOutput 'inkflow-engine.exe'))) {
    throw 'The Python sidecar build finished without producing inkflow-engine.exe.'
}

New-Item -ItemType Directory -Force -Path $extensionBin | Out-Null
New-Item -ItemType Directory -Force -Path $extensionRelease | Out-Null
New-Item -ItemType Directory -Force -Path $desktopRelease | Out-Null
Copy-Item -LiteralPath (Join-Path $engineOutput 'inkflow-engine.exe') -Destination (Join-Path $extensionBin 'inkflow-engine.exe') -Force

Push-Location (Join-Path $repositoryRoot 'vscode-extension')
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw "VS Code dependency installation failed with exit code $LASTEXITCODE." }
    npm run package
    if ($LASTEXITCODE -ne 0) { throw "VSIX packaging failed with exit code $LASTEXITCODE." }
}
finally {
    Pop-Location
}

Push-Location (Join-Path $repositoryRoot 'desktop')
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw "Desktop dependency installation failed with exit code $LASTEXITCODE." }
    npm run dist:win
    if ($LASTEXITCODE -ne 0) { throw "Desktop packaging failed with exit code $LASTEXITCODE." }
}
finally {
    Pop-Location
}

Write-Host 'InkFlow 0.2 build completed: desktop\release and vscode-extension\release.'
