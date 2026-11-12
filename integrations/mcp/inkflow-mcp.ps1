$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$agentRoot = Join-Path $repositoryRoot 'agent'
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
