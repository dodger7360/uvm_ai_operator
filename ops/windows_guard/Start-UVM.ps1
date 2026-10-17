$ErrorActionPreference = 'Stop'
$manifest = Join-Path $PSScriptRoot 'runtime.json'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'Guard manifest missing; no fallback' }
$config = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
& $config.guard_python -I -S -B (Join-Path $PSScriptRoot 'guard.py') start --manifest $manifest --initial-attempts $StartupAttempts
if ($LASTEXITCODE -ne 0) { throw 'Guard refused launch; maintenance retained when preflight failed' }
param([ValidateRange(1,10)][int]$StartupAttempts = 2)
