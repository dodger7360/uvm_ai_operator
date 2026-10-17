param([ValidateRange(1,10)][int]$StartupAttempts = 2)
$ErrorActionPreference = 'Stop'
$guardStart = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'UVM_Steel-Guard-v1\Start-UVM.ps1'
if (-not (Test-Path -LiteralPath $guardStart -PathType Leaf)) { throw 'Permanent startup guard is not installed; no checkout fallback' }
& $guardStart -StartupAttempts $StartupAttempts
if ($LASTEXITCODE -ne 0) { throw 'Permanent startup guard refused launch' }
