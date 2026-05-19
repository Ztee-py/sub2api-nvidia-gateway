param(
    [string]$EnvFile = "$PSScriptRoot\wechat_windows_watcher.env",
    [string]$WechatDecryptAppDir = "C:\Users\86199\AppData\Local\Temp\wechat-decrypt-ylytdeng",
    [double]$RefreshIntervalSeconds = 3,
    [switch]$InstallStartupTask,
    [switch]$UninstallStartupTask,
    [string]$TaskName = "ZteAPI WeChat QRPay Stack"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Test-ProcessCommandLine {
    param([string]$Pattern)
    return [bool](Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $Pattern } | Select-Object -First 1)
}

function Resolve-EnvFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Env file not found: $Path. Copy wechat_windows_watcher.env.example to wechat_windows_watcher.env and fill the secret."
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Quote-Arg {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

if ($UninstallStartupTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "ZteAPIWeChatQRPayStack" -ErrorAction SilentlyContinue
    Write-Host "uninstalled startup task: $TaskName"
    return
}

if ($InstallStartupTask) {
    $resolvedEnvFile = Resolve-EnvFile -Path $EnvFile
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", (Quote-Arg $PSCommandPath),
        "-EnvFile", (Quote-Arg $resolvedEnvFile),
        "-WechatDecryptAppDir", (Quote-Arg $WechatDecryptAppDir),
        "-RefreshIntervalSeconds", "$RefreshIntervalSeconds"
    ) -join " "
    try {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settingsArgs = @{
            AllowStartIfOnBatteries = $true
            RestartCount = 3
            RestartInterval = New-TimeSpan -Minutes 1
        }
        if ((Get-Command New-ScheduledTaskSettingsSet).Parameters.ContainsKey("DisallowStartIfOnBatteries")) {
            $settingsArgs.DisallowStartIfOnBatteries = $false
        }
        $settings = New-ScheduledTaskSettingsSet @settingsArgs
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Starts the ZteAPI WeChat decrypt refresher and QR payment watcher after Windows logon." -Force | Out-Null
        Write-Host "installed startup task: $TaskName"
    }
    catch {
        $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        $runName = "ZteAPIWeChatQRPayStack"
        $runCommand = "powershell.exe $arguments"
        New-Item -Path $runKey -Force | Out-Null
        Set-ItemProperty -Path $runKey -Name $runName -Value $runCommand
        Write-Host "scheduled task install failed; installed current-user Run startup entry: $runName"
    }
    return
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
