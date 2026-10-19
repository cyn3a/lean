[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PipelineArgs
)

$ErrorActionPreference = 'Stop'
$bundledPython = 'C:\Users\Cynthia\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

if (Test-Path -LiteralPath $bundledPython) {
    $python = $bundledPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = (Get-Command py).Source
} else {
    throw 'Python 3.11 or newer was not found.'
}

& $python (Join-Path $PSScriptRoot 'run_pipeline.py') @PipelineArgs
exit $LASTEXITCODE
