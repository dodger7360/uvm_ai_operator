$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Process -FilePath (Join-Path $projectDir '.venv\Scripts\pythonw.exe') -ArgumentList (Join-Path $projectDir 'main.py') -WorkingDirectory $projectDir -WindowStyle Hidden
