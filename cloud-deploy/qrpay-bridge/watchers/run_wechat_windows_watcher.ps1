param(
    [string]$EnvFile = "$PSScriptRoot\wechat_windows_watcher.env",
    [switch]$DryRun,
    [switch]$Once,
    [switch]$SendRawText,
    [switch]$InstallStartupTask,
    [switch]$UninstallStartupTask,
    [switch]$StartHidden,
    [string]$TaskName = "ZteAPI WeChat Watcher"
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

function Build-PowerShellArguments {
    param(
        [string]$ResolvedEnvFile,
        [bool]$Hidden,
        [bool]$IncludeRunSwitches
    )
    $items = @("-NoProfile", "-ExecutionPolicy", "Bypass")
    if ($Hidden) {
        $items += @("-WindowStyle", "Hidden")
    }
    $items += @("-File", (Quote-Arg $PSCommandPath), "-EnvFile", (Quote-Arg $ResolvedEnvFile))
    if ($IncludeRunSwitches) {
        if ($DryRun) { $items += "-DryRun" }
        if ($Once) { $items += "-Once" }
        if ($SendRawText) { $items += "-SendRawText" }
    }
    return ($items -join " ")
}

if ($UninstallStartupTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "uninstalled startup task: $TaskName"
    return
}

if ($InstallStartupTask) {
    $resolvedEnvFile = Resolve-EnvFile -Path $EnvFile
    $arguments = Build-PowerShellArguments -ResolvedEnvFile $resolvedEnvFile -Hidden $true -IncludeRunSwitches $false
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DisallowStartIfOnBatteries:$false -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Runs the ZteAPI WeChat QR payment watcher after Windows logon." -Force | Out-Null
    Write-Host "installed startup task: $TaskName"
    Write-Host "it will start hidden after this Windows user logs in; run manually once now with .\run_wechat_windows_watcher.ps1 if you want immediate listening."
    return
}

if ($StartHidden) {
    $resolvedEnvFile = Resolve-EnvFile -Path $EnvFile
    $arguments = Build-PowerShellArguments -ResolvedEnvFile $resolvedEnvFile -Hidden $true -IncludeRunSwitches $true
    Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden
    Write-Host "started watcher in a hidden PowerShell window"
    return
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
