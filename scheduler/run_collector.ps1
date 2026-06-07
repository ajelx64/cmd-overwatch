# run_collector.ps1 — scheduled-task wrapper for one collector pass.
. "$PSScriptRoot\_common.ps1"
exit (Invoke-OverwatchModule -Name 'collector' -Module 'overwatch.collector')
