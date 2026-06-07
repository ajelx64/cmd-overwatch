#Requires -Version 7.0
<#
.SYNOPSIS
Remove all overwatch scheduled tasks (default folder '\Overwatch\').
#>
[CmdletBinding()]
param(
    [string] $TaskFolder = '\Overwatch\'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Elevation required — relaunching as administrator...'
    Start-Process -FilePath (Get-Command pwsh).Source -Verb RunAs -Wait -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-TaskFolder', $TaskFolder)
    return
}

$tasks = Get-ScheduledTask -TaskPath $TaskFolder -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "No tasks found under $TaskFolder"
    return
}
$tasks | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -Confirm:$false
    Write-Host ("Removed {0}{1}" -f $_.TaskPath, $_.TaskName)
}
