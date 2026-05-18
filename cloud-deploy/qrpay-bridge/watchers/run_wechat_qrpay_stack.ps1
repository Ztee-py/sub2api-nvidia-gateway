param(
    [string]$EnvFile = "$PSScriptRoot\wechat_windows_watcher.env",
    [string]$WechatDecryptAppDir = "C:\Users\86199\AppData\Local\Temp\wechat-decrypt-ylytdeng",
    [double]$RefreshIntervalSeconds = 3
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Test-ProcessCommandLine {
    param([string]$Pattern)
    return [bool](Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $Pattern } | Select-Object -First 1)
}

$refreshScript = Join-Path $PSScriptRoot "run_wechat_decrypt_refresher.ps1"
$watcherScript = Join-Path $PSScriptRoot "run_wechat_windows_watcher.ps1"

if (-not (Test-ProcessCommandLine "wechat_decrypt_message_refresher.py")) {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $refreshScript -WechatDecryptAppDir $WechatDecryptAppDir -IntervalSeconds $RefreshIntervalSeconds -StartHidden
}

if (-not (Test-ProcessCommandLine "wechat_windows_watcher.py")) {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watcherScript -EnvFile $EnvFile -StartHidden
}

Write-Host "qrpay WeChat stack is running"
