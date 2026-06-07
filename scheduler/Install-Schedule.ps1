#Requires -Version 7.0
<#
.SYNOPSIS
Register the overwatch collector as a recurring Windows Scheduled Task.

.DESCRIPTION
Creates a task under the given task folder (default '\Overwatch\') that runs
`python -m overwatch.collector` on an interval (default every 30 minutes).
Detection findings land in the SQLite store; the dashboard server reads them,
so the health board stays current even when the server itself is down.

Uses an S4U principal: the task runs whether or not you are logged on, with no
stored password and Limited (non-elevated) rights. The collector is read-only
apart from its own data directory. Registration itself needs admin, so this
self-elevates. Re-running is idempotent (existing task is replaced).

.EXAMPLE
pwsh -NoProfile -File scheduler\Install-Schedule.ps1
pwsh -NoProfile -File scheduler\Install-Schedule.ps1 -IntervalMinutes 15
#>
[CmdletBinding()]
param(
    [string] $TaskFolder      = '\Overwatch\',
    [int]    $IntervalMinutes = 30,
    [string] $StartAt         = '06:00',
    [string] $AarAt           = '07:30'   # daily after-action report
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- self-elevate: task registration needs admin -------------------------------
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Elevation required (task registration) — relaunching as administrator...'
    $relaunch = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
                  '-TaskFolder', $TaskFolder, '-IntervalMinutes', $IntervalMinutes,
                  '-StartAt', $StartAt, '-AarAt', $AarAt)
    Start-Process -FilePath (Get-Command pwsh).Source -Verb RunAs -Wait -ArgumentList $relaunch
    return
}

$root   = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logDir = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
try {
    Start-Transcript -Path (Join-Path $logDir ("install-schedule-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))) -Append | Out-Null
} catch {}

$pwshPath = (Get-Command pwsh).Source
$user     = "$env:USERDOMAIN\$env:USERNAME"

# S4U = "Run whether user is logged on or not" without storing a password.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

$action = New-ScheduledTaskAction -Execute $pwshPath -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'run_collector.ps1')
) -WorkingDirectory $root

# Repeat all day, every day; StartWhenAvailable catches up missed runs after wake.
$trigger = New-ScheduledTaskTrigger -Once -At $StartAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskPath $TaskFolder -TaskName 'Collector' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# Daily AAR: one run each morning, after the early scheduled jobs have reported in.
$aarAction = New-ScheduledTaskAction -Execute $pwshPath -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'run_aar.ps1')
) -WorkingDirectory $root
$aarTrigger  = New-ScheduledTaskTrigger -Daily -At $AarAt
$aarSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskPath $TaskFolder -TaskName 'Daily AAR' `
    -Action $aarAction -Trigger $aarTrigger -Principal $principal -Settings $aarSettings -Force | Out-Null

Write-Host ("Registered {0}Collector — every {1} min (S4U, Limited)." -f $TaskFolder, $IntervalMinutes)
Write-Host ("Registered {0}Daily AAR — daily at {1} (S4U, Limited)." -f $TaskFolder, $AarAt)
Get-ScheduledTask -TaskPath $TaskFolder | Format-Table TaskName, State -AutoSize
try { Stop-Transcript | Out-Null } catch {}
