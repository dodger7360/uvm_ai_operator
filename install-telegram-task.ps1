$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'UVM_SteelBot'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) { throw 'A task with this name already exists. Review it before updating; nothing overwritten.' }
$action = New-ScheduledTaskAction -Execute (Join-Path $projectDir '.venv\Scripts\pythonw.exe') -Argument ('"' + (Join-Path $projectDir 'windows_supervisor.py') + '"') -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'UVM_Steel: current-user logon; OS locks; bounded child restart; maintenance marker' | Out-Null
Write-Output 'Registered UVM_SteelBot at current-user logon, without elevation or stored password'
