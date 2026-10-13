param([ValidateRange(1,1800)][int]$TimeoutSeconds = 180)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
[System.IO.File]::WriteAllText((Join-Path $projectDir 'telegram.maintenance'), 'maintenance')
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $processes = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and
        ($_.CommandLine -like ('*' + $projectDir + '\main.py*') -or $_.CommandLine -like ('*' + $projectDir + '\windows_supervisor.py*'))
    })
    if ($processes.Count -eq 0) { Write-Output 'Bot stopped; maintenance marker retained'; return }
    Start-Sleep -Seconds 1
} while ([DateTime]::UtcNow -lt $deadline)
throw 'Bot still finishing work. No process was killed; keep maintenance marker and inspect safely.'
