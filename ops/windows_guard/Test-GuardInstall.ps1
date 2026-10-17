param([Parameter(Mandatory)][string]$Directory,[Parameter(Mandatory)][string]$RuntimeAccount)
$ErrorActionPreference = 'Stop'
$runtimeSid = ([Security.Principal.NTAccount]::new($RuntimeAccount)).Translate([Security.Principal.SecurityIdentifier]).Value
$writers = @('S-1-5-32-544','S-1-5-18')
$writeMask = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
foreach ($item in @((Get-Item -LiteralPath $Directory)) + @(Get-ChildItem -LiteralPath $Directory -Recurse)) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point in protected installation' }
    $acl = Get-Acl -LiteralPath $item.FullName
    $owner = ([Security.Principal.NTAccount]::new($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $writers) { throw 'Unprotected owner' }
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $writeMask) -and $sid -notin $writers) { throw 'Non-administrator write access' }
    }
}
if (-not (Get-Acl -LiteralPath $Directory).AreAccessRulesProtected) { throw 'Inherited root ACL is not permitted' }
Write-Output 'guard_acl_ok'
