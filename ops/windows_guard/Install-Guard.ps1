param(
    [Parameter(Mandatory)][string]$Destination,
    [Parameter(Mandatory)][string]$RuntimeAccount,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$ApprovedSourceSha
)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Elevated installer required' }
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$target = [IO.Path]::GetFullPath($Destination)
$programData = [Environment]::GetFolderPath('CommonApplicationData')
if ((Split-Path -Parent $target) -ne $programData) { throw 'Use a new direct child of protected ProgramData, not a user-owned parent' }
if ($target.StartsWith($sourceRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Guard must be outside checkout' }
if (Test-Path -LiteralPath $target) { throw 'Use a new versioned directory; never overwrite an installed guard' }
$actualSha = & git -C $sourceRoot rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualSha -ne $ApprovedSourceSha) { throw 'Unapproved source SHA' }
$dirty = & git -C $sourceRoot status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Source checkout must be clean' }
foreach ($relative in @('ops/windows_guard/guard.py','ops/windows_guard/Start-UVM.ps1','ops/windows_guard/Test-GuardInstall.ps1','windows_supervisor.py','windows_runtime.py')) {
    & git -C $sourceRoot ls-files --error-unmatch -- $relative | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Bundle source must be tracked in the approved commit' }
}
$runtimeSid = ([Security.Principal.NTAccount]::new($RuntimeAccount)).Translate([Security.Principal.SecurityIdentifier])
$admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
New-Item -ItemType Directory -Path $target | Out-Null
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true,$false)
$acl.SetOwner($admins)
foreach ($sid in @($admins,$system)) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($runtimeSid,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))
Set-Acl -LiteralPath $target -AclObject $acl
foreach ($name in @('guard.py','Start-UVM.ps1','Test-GuardInstall.ps1')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $target $name)
}
foreach ($name in @('windows_supervisor.py','windows_runtime.py')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot $name) -Destination (Join-Path $target $name)
}
foreach ($file in Get-ChildItem -LiteralPath $target -File) {
    $fileAcl = Get-Acl -LiteralPath $file.FullName
    $fileAcl.SetOwner($admins)
    Set-Acl -LiteralPath $file.FullName -AclObject $fileAcl
}
& (Join-Path $target 'Test-GuardInstall.ps1') -Directory $target -RuntimeAccount $RuntimeAccount
if ($LASTEXITCODE -ne 0) { throw 'Guard ACL verification failed' }
Write-Output 'Bundle installed only. Manifest, launch-point cutover and queue activation are separate offline approvals.'
