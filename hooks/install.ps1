# install.ps1 — Configure Claude Code hooks for Claude Overwatch

$captureScript = Join-Path $PSScriptRoot "capture.py"
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

# Read existing settings or start fresh
if (Test-Path $settingsPath) {
    $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json -AsHashtable
} else {
    $settings = @{}
    New-Item -ItemType Directory -Force -Path (Split-Path $settingsPath) | Out-Null
}

if (-not $settings.ContainsKey("hooks")) {
    $settings["hooks"] = @{}
}

$hooks = $settings["hooks"]
$pyCmd = "python `"$captureScript`""

# Helper: check if a hook command is already registered (idempotent)
function Test-HookExists($hookArray, $cmdSnippet) {
    foreach ($entry in $hookArray) {
        if ($entry -is [hashtable] -and $entry.ContainsKey("command") -and $entry["command"] -like "*$cmdSnippet*") { return $true }
        if ($entry -is [hashtable] -and $entry.ContainsKey("hooks")) {
            foreach ($inner in $entry["hooks"]) {
                if ($inner["command"] -like "*$cmdSnippet*") { return $true }
            }
        }
    }
    return $false
}

# Append to existing hook arrays rather than replace, preserving user's other hooks
if (-not $hooks.ContainsKey("PreToolUse")) { $hooks["PreToolUse"] = @() }
if (-not (Test-HookExists $hooks["PreToolUse"] "capture.py")) {
    $hooks["PreToolUse"] += @{ matcher = ""; hooks = @(@{ type = "command"; command = "$pyCmd pre" }) }
}

if (-not $hooks.ContainsKey("PostToolUse")) { $hooks["PostToolUse"] = @() }
if (-not (Test-HookExists $hooks["PostToolUse"] "capture.py")) {
    $hooks["PostToolUse"] += @{ matcher = ""; hooks = @(@{ type = "command"; command = "$pyCmd post" }) }
}

if (-not $hooks.ContainsKey("Stop")) { $hooks["Stop"] = @() }
if (-not (Test-HookExists $hooks["Stop"] "capture.py")) {
    $hooks["Stop"] += @{ type = "command"; command = "$pyCmd stop" }
}

$settings | ConvertTo-Json -Depth 10 | Set-Content $settingsPath -Encoding UTF8

Write-Host "Claude Overwatch hooks installed to: $settingsPath"
Write-Host "Capture script: $captureScript"
Write-Host ""
Write-Host "Start Claude Overwatch: cd '$((Get-Item $PSScriptRoot).Parent.FullName)' && .\start.ps1"
