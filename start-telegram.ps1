param([ValidateRange(1,10)][int]$StartupAttempts = 2)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$marker = Join-Path $projectDir 'telegram.maintenance'
if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
Start-Process -FilePath (Join-Path $projectDir '.venv\Scripts\pythonw.exe') -ArgumentList @(('"' + (Join-Path $projectDir 'windows_supervisor.py') + '"'), '--initial-attempts', $StartupAttempts) -WorkingDirectory $projectDir -WindowStyle Hidden
