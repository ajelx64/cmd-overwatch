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

$hooks["PreToolUse"] = @(
    @{
        matcher = ""
        hooks = @(@{ type = "command"; command = "$pyCmd pre" })
    }
)

$hooks["PostToolUse"] = @(
    @{
        matcher = ""
        hooks = @(@{ type = "command"; command = "$pyCmd post" })
    }
)

$hooks["Stop"] = @(
    @{ type = "command"; command = "$pyCmd stop" }
)

$settings | ConvertTo-Json -Depth 10 | Set-Content $settingsPath -Encoding UTF8

Write-Host "Claude Overwatch hooks installed to: $settingsPath"
Write-Host "Capture script: $captureScript"
Write-Host ""
Write-Host "Start Claude Overwatch: cd '$((Get-Item $PSScriptRoot).Parent.FullName)' && .\start.ps1"
