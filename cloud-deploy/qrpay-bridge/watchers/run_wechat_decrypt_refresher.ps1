param(
    [string]$WechatDecryptAppDir = $env:WECHAT_DECRYPT_APP_DIR,
    [double]$IntervalSeconds = 3,
    [switch]$Once,
    [switch]$StartHidden
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

if (-not $WechatDecryptAppDir) {
    $WechatDecryptAppDir = "C:\Users\86199\AppData\Local\Temp\wechat-decrypt-ylytdeng"
}

if ($StartHidden) {
    $argsList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", "`"$PSCommandPath`"",
        "-WechatDecryptAppDir", "`"$WechatDecryptAppDir`"",
        "-IntervalSeconds", "$IntervalSeconds"
    )
    if ($Once) {
        $argsList += "-Once"
    }
    Start-Process -FilePath "powershell.exe" -ArgumentList ($argsList -join " ") -WindowStyle Hidden
    Write-Host "started wechat-decrypt refresher in a hidden PowerShell window"
    return
}

$env:WECHAT_DECRYPT_APP_DIR = $WechatDecryptAppDir
$env:WECHAT_DECRYPT_REFRESH_INTERVAL_SECONDS = "$IntervalSeconds"
$script = Join-Path $PSScriptRoot "wechat_decrypt_message_refresher.py"
$pythonArgs = @($script, "--wechat-decrypt-app-dir", $WechatDecryptAppDir, "--interval", "$IntervalSeconds")
if ($Once) {
    $pythonArgs += "--once"
}
python @pythonArgs
