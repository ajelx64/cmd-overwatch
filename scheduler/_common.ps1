# _common.ps1 — shared helpers for the overwatch scheduler wrappers.
#
# Dot-source from the run_*.ps1 wrappers:  . "$PSScriptRoot\_common.ps1"
# Resolves the repo root, picks a Python interpreter, and runs an overwatch
# module while tee-ing all output to a dated log under data/logs/ (the only
# place you'll see output when the task runs logged-off in Session 0).
#
# Test-PwshLaunches / Get-StablePwshPath below are VENDORED from the Command's canonical
# guild/master/Get-StablePwsh.ps1 (not dot-sourced — this repo is public and independently
# cloneable, and must work standalone with no assumption that a sibling "guild" tree exists).
# If that canonical logic changes, update this copy by hand.

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

function Test-PwshLaunches {
    <# Existence is not launchability. A stale or half-uninstalled MSI leaves a pwsh.exe that
       Test-Path happily confirms but that cannot run - and registering it reproduces the exact
       bug this whole change exists to kill: task registers green, run dies before the runner
       script can log anything, failure is silent. So prove the candidate actually executes.
       Caveat: this proves launchability in the INSTALLER's interactive context, not under the
       task's S4U principal - it narrows the risk, it does not retire the post-install live check. #>
    [CmdletBinding()] param([Parameter(Mandatory)][string] $Path)
    try {
        $major = & $Path -NoProfile -NonInteractive -Command '$PSVersionTable.PSVersion.Major' 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        return ([int]($major | Select-Object -First 1) -ge 7)   # #Requires -Version 7.0
    } catch { return $false }
}

function Get-StablePwshPath {
    <# Resolve an interpreter path that SURVIVES PowerShell updates.

       (Get-Command pwsh).Source returns the version-pinned Store path, e.g.
         ...\WindowsApps\Microsoft.PowerShell_7.6.2.0_x64__8wekyb3d8bbwe\pwsh.exe
       Baking that into a scheduled task is a time bomb: the Store auto-updates
       (7.6.2.0 -> 7.6.4.0), the versioned directory disappears, and the task fails
       with 0x80070002 (file-not-found) BEFORE the runner script starts - so it can never
       write the log that would report the failure. A task broken this way is silent for as
       long as nobody checks its output, because there is no output to check - and this same
       pattern had already been copied into the collector's own scheduled tasks.

       Preference order, most robust first:
         1. A real MSI/zip install (...\PowerShell\7\pwsh.exe under $env:ProgramFiles). Stable
            across updates AND free of the MSIX app-model, so it needs no package registration
            for the launching token - the safest thing to hand a non-interactive S4U task.
         2. The App Execution Alias. Also version-stable, but it is a 0-byte AppExecLink reparse
            point resolved via package registration, so it is the less-proven target of the two.
         3. Nothing - throw. Never fall through to a versioned path; that re-arms the same bomb.
       The guard below rejects ANY versioned WindowsApps package folder, not just the exact
       "Microsoft.PowerShell_" name, so preview/LTS package variants cannot slip past it. #>
    [CmdletBinding()] param()

    # String concatenation, NOT Join-Path: under $ErrorActionPreference='Stop', Join-Path throws
    # DriveNotFoundException when an env var points at a drive that no longer exists, which would
    # abort resolution instead of falling through to the next candidate. Test-Path itself never
    # throws for a bad drive - it just returns $false - so the guard belongs here, not on it.
    $msi = "$env:ProgramFiles\PowerShell\7\pwsh.exe"
    if ((Test-Path -LiteralPath $msi) -and (Test-PwshLaunches $msi)) { return $msi }

    $alias = "$env:LOCALAPPDATA\Microsoft\WindowsApps\pwsh.exe"
    if ((Test-Path -LiteralPath $alias) -and (Test-PwshLaunches $alias)) { return $alias }

    # Two statements, not one: under Set-StrictMode -Version Latest, reading .Source off a null
    # Get-Command result throws PropertyNotFoundException, which would make the actionable
    # message below unreachable - on a function whose entire job is to fail legibly.
    $cmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'pwsh.exe not found (no MSI install, no App Execution Alias, not on PATH).' }
    $fallback = $cmd.Source

    if ($fallback -match '\\WindowsApps\\[^\\]*_\d+\.\d+\.\d+') {
        throw ("Only a version-pinned pwsh path is available ($fallback). Registering it would " +
               'break again on the next PowerShell update. Enable the "pwsh" App Execution Alias ' +
               '(Settings > Apps > Advanced app settings > App execution aliases), or install ' +
               'PowerShell via MSI to C:\Program Files\PowerShell\7\, then re-run.')
    }
    $fallback
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
