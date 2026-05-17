param(
    [string]$EnvFile = "$PSScriptRoot\wechat_windows_watcher.env",
    [switch]$DryRun,
    [switch]$Once,
    [switch]$SendRawText
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

function Load-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Env file not found: $Path. Copy wechat_windows_watcher.env.example to wechat_windows_watcher.env and fill the secret."
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -ne 2) {
            continue
        }
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
    }
}

Load-DotEnv -Path $EnvFile

$script = Join-Path $PSScriptRoot "wechat_windows_watcher.py"
$argsList = @($script)
if ($DryRun) {
    $argsList += "--dry-run"
}
if ($Once) {
    $argsList += "--once"
}
if ($SendRawText) {
    $argsList += "--send-raw-text"
}

python @argsList
