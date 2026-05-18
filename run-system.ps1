param(
    [string]$RepoRoot = $PSScriptRoot,
    [string]$WechatDecryptAppDir = "C:\Users\86199\AppData\Local\Temp\wechat-decrypt-ylytdeng",
    [string]$ServerHost = $env:ZTEAPI_SERVER_HOST,
    [string]$ServerUser = "root",
    [switch]$SkipWindowsWatcher,
    [switch]$SkipServer,
    [switch]$OpenServerLogin
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "== $Text =="
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
Set-Location $RepoRoot

if (-not $SkipWindowsWatcher) {
    Write-Step "Starting Windows WeChat watcher stack"
    $stack = Join-Path $RepoRoot "cloud-deploy\qrpay-bridge\watchers\run_wechat_qrpay_stack.ps1"
    if (-not (Test-Path -LiteralPath $stack)) {
        throw "Watcher stack script not found: $stack"
    }
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stack -WechatDecryptAppDir $WechatDecryptAppDir
}

Write-Step "Checking public QRPay watcher status"
try {
    curl.exe -fsS "https://Zteapi.com/qrpay/api/watch/public-status"
    Write-Host ""
} catch {
    Write-Warning "Could not read public watcher status yet: $($_.Exception.Message)"
}

if (-not $SkipServer) {
    Write-Step "Server deploy path"
    if (-not $ServerHost) {
        Write-Warning "ServerHost is empty. Pass -ServerHost <ip-or-domain> or set ZTEAPI_SERVER_HOST before deploying."
        Write-Host "Example:"
        Write-Host '  $env:ZTEAPI_SERVER_HOST = "YOUR_SERVER_IP"'
        Write-Host "  .\run-system.ps1 -OpenServerLogin"
        Write-Step "Done"
        return
    }
    $remoteCommand = "cd /opt/sub2api-nvidia && git pull --ff-only && cd cloud-deploy && ./scripts/backup.sh && docker compose build qrpay-bridge html-injector && docker compose up -d qrpay-bridge html-injector && docker compose restart caddy && ./scripts/health-check.sh"
    Write-Host "Use this SSH command when you are ready to deploy:"
    Write-Host ""
    Write-Host "ssh $ServerUser@$ServerHost `"$remoteCommand`""
    Write-Host ""
    if ($OpenServerLogin) {
        Write-Host "Opening SSH. After you enter the password, the deploy command will run automatically:"
        Write-Host $remoteCommand
        ssh "$ServerUser@$ServerHost" "$remoteCommand"
    } else {
        Write-Host "Add -OpenServerLogin to open the SSH login prompt from this script."
    }
}

Write-Step "Done"
