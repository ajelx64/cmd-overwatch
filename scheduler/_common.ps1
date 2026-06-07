# _common.ps1 — shared helpers for the overwatch scheduler wrappers.
#
# Dot-source from the run_*.ps1 wrappers:  . "$PSScriptRoot\_common.ps1"
# Resolves the repo root, picks a Python interpreter, and runs an overwatch
# module while tee-ing all output to a dated log under data/logs/ (the only
# place you'll see output when the task runs logged-off in Session 0).

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# scheduler/_common.ps1 -> .. == the repo root
$script:OverwatchRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Get-OverwatchPython {
    <# Prefer a repo venv; fall back to system python / py launcher. #>
    $venv = Join-Path $script:OverwatchRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    foreach ($name in 'python', 'py') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw 'No Python interpreter found (.venv\Scripts\python.exe, python, or py).'
}

function Invoke-OverwatchModule {
    <# Run `python -m <Module>`, tee stdout+stderr to data/logs/<Name>-<date>.log,
       return its exit code. #>
    param(
        [Parameter(Mandatory)] [string]   $Name,    # log label, e.g. 'collector'
        [Parameter(Mandatory)] [string]   $Module,  # e.g. 'overwatch.collector'
        [string[]] $ModuleArgs = @()
    )
    if ($null -eq $ModuleArgs) { $ModuleArgs = @() }
    $logDir = Join-Path $script:OverwatchRoot 'data\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $log = Join-Path $logDir ('{0}-{1}.log' -f $Name, (Get-Date -Format 'yyyy-MM-dd'))

    $python = Get-OverwatchPython
    $stamp  = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
    # Tee writes to the log AND passes objects through; route the passthrough to
    # Out-Host so it does NOT pollute this function's return value.
    "=== $stamp  start $Name  ($python -m $Module $($ModuleArgs -join ' ')) ===" |
        Tee-Object -FilePath $log -Append | Out-Host

    Push-Location $script:OverwatchRoot
    try {
        $ErrorActionPreference = 'Continue'
        & $python -m $Module @ModuleArgs 2>&1 | Tee-Object -FilePath $log -Append | Out-Host
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    "=== exit $code @ $(Get-Date -Format 'HH:mm:ss') ===" | Tee-Object -FilePath $log -Append | Out-Host
    return $code
}
