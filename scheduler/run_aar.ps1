# run_aar.ps1 — scheduled-task wrapper for the daily after-action report.
. "$PSScriptRoot\_common.ps1"
exit (Invoke-OverwatchModule -Name 'aar' -Module 'overwatch.aar')
