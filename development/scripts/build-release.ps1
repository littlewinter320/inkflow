param(
    [switch]$Publish,
    [switch]$IncludeExtension
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $repositoryRoot '.venv\Scripts\pyinstaller.exe'
$engineOutput = Join-Path $repositoryRoot 'dist\engine'
$agentRoot = Join-Path $repositoryRoot 'agent'
$extensionBin = Join-Path $repositoryRoot 'extension\bin'
$extensionRelease = Join-Path $repositoryRoot 'artifacts\extension'
$desktopRelease = Join-Path $repositoryRoot 'artifacts\desktop'
$systemPowerShellDirectory = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0'
$system32Directory = Join-Path $env:SystemRoot 'System32'

# electron-builder invokes powershell.exe and cmd.exe by name. Some IDE-hosted
# Node runtimes omit the Windows system directories from PATH.
$env:PATH = "$systemPowerShellDirectory;$system32Directory;$env:PATH"

$desktopVersion = (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $repositoryRoot 'desktop\package.json') | ConvertFrom-Json).version
$agentVersion = (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $agentRoot 'pyproject.toml') | Select-String -Pattern 'version\s*=\s*"([^"]+)"').Matches.Groups[1].Value
if (-not $desktopVersion -or $desktopVersion -ne $agentVersion) {
    throw "Desktop and agent versions must match before publishing: desktop=$desktopVersion agent=$agentVersion."
}

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Create the Python virtual environment in the repository root first.'
}

& $python -m pip install -e "${agentRoot}[build,lightvoice]"
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed with exit code $LASTEXITCODE."
}

& $pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name inkflow-engine `
    --paths (Join-Path $agentRoot 'src') `
    --collect-submodules mcp.server `
    --collect-submodules mcp.shared `
    --collect-data mcp `
    --collect-all sherpa_onnx `
    --hidden-import soundfile `
    --hidden-import mcp.types `
    --exclude-module mcp.cli `
    --distpath $engineOutput `
    --workpath (Join-Path $repositoryRoot 'build\pyinstaller') `
    --specpath (Join-Path $repositoryRoot 'build\pyinstaller') `
    (Join-Path $agentRoot 'scripts\inkflow_app_server_entry.py')
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath (Join-Path $engineOutput 'inkflow-engine.exe'))) {
    throw 'The Python sidecar build finished without producing inkflow-engine.exe.'
}

New-Item -ItemType Directory -Force -Path $desktopRelease | Out-Null
if ($IncludeExtension) {
    New-Item -ItemType Directory -Force -Path $extensionBin | Out-Null
    New-Item -ItemType Directory -Force -Path $extensionRelease | Out-Null
    Copy-Item -LiteralPath (Join-Path $engineOutput 'inkflow-engine.exe') -Destination (Join-Path $extensionBin 'inkflow-engine.exe') -Force
    Push-Location (Join-Path $repositoryRoot 'extension')
    try {
        npm install
        if ($LASTEXITCODE -ne 0) { throw "VS Code dependency installation failed with exit code $LASTEXITCODE." }
        npm run package
        if ($LASTEXITCODE -ne 0) { throw "VSIX packaging failed with exit code $LASTEXITCODE." }
    }
    finally {
        Pop-Location
    }
}

Push-Location (Join-Path $repositoryRoot 'desktop')
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw "Desktop dependency installation failed with exit code $LASTEXITCODE." }
    if ($Publish) {
        $tag = "v$desktopVersion"
        $tagExists = (& git ls-remote --tags origin $tag)
        if (-not $tagExists) {
            throw "Publishing requires an existing pushed Git tag $tag. Create and push the tag first, then rerun this script."
        }
        $releaseToken = (& gh auth token).Trim()
        if (-not $releaseToken) { throw 'GitHub login token is unavailable; cannot create a public Release.' }
        $env:GH_TOKEN = $releaseToken
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Desktop build failed with exit code $LASTEXITCODE." }
        npx electron-builder --win nsis --publish never
        if ($LASTEXITCODE -ne 0) { throw "Desktop package generation failed with exit code $LASTEXITCODE." }
        $desktopAssets = @(Get-ChildItem -LiteralPath (Join-Path $desktopRelease $desktopVersion) -File | Where-Object {
            $_.Name -eq "InkFlow-Setup-$desktopVersion.exe" -or
            $_.Name -eq "InkFlow-Setup-$desktopVersion.exe.blockmap" -or
            $_.Name -eq "latest.yml"
        } | ForEach-Object { $_.FullName })
        if (-not ($desktopAssets | Where-Object { $_ -like '*latest.yml' }) -or -not ($desktopAssets | Where-Object { $_ -like '*.exe' })) {
            throw 'The desktop build did not produce the installer and latest.yml required by electron-updater.'
        }
        & gh release view $tag *> $null
        if ($LASTEXITCODE -ne 0) {
            & gh release create $tag --title ('InkFlow desktop {0}' -f $desktopVersion) --notes ('InkFlow desktop release {0}.' -f $desktopVersion)
            if ($LASTEXITCODE -ne 0) { throw "GitHub Release creation failed with exit code $LASTEXITCODE." }
        }
        & gh release upload $tag @desktopAssets --clobber
        if ($LASTEXITCODE -ne 0) { throw "GitHub Release asset upload failed with exit code $LASTEXITCODE." }
    }
    else {
        npm run dist:win
    }
    if ($LASTEXITCODE -ne 0) { throw "Desktop packaging failed with exit code $LASTEXITCODE." }
}
finally {
    Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
    Pop-Location
}

$extensionNote = if ($IncludeExtension) { ' and artifacts\extension' } else { '' }
Write-Host ('InkFlow desktop {0} build completed: artifacts\desktop{1}.' -f $desktopVersion, $extensionNote)
