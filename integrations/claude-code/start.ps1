param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArguments
)

$ErrorActionPreference = 'Stop'
$inkflowRepository = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$inkflowRules = Join-Path $inkflowRepository 'AGENTS.md'
$inkflowClaude = Get-Command claude -ErrorAction Stop

Push-Location $inkflowRepository
try {
    & $inkflowClaude --append-system-prompt-file $inkflowRules @ClaudeArguments
    $inkflowExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $inkflowExitCode
