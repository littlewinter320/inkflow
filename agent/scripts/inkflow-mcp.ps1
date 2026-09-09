$ErrorActionPreference = 'Stop'
$agentRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $agentRoot
$venvPython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$env:PYTHONPATH = Join-Path $agentRoot 'src'
$env:INKFLOW_WORKSPACE = $repositoryRoot
& $python -m inkflow.mcp_server
exit $LASTEXITCODE
