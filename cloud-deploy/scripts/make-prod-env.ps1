$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$deploy = Resolve-Path (Join-Path $PSScriptRoot "..")
$rootEnv = Join-Path $root ".env"
$targetEnv = Join-Path $deploy ".env"

if (!(Test-Path $rootEnv)) {
  throw "Root .env not found: $rootEnv"
}

function Read-EnvValue([string]$path, [string]$name) {
  $line = Get-Content $path | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
  if (!$line) { return "" }
  return $line -replace "^$name=", ""
}

function New-Token([int]$bytes = 32) {
  $raw = New-Object byte[] $bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($raw)
  return [Convert]::ToBase64String($raw).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$nvidiaKeys = Read-EnvValue $rootEnv "NVIDIA_API_KEYS"
if (!$nvidiaKeys) {
  throw "NVIDIA_API_KEYS is empty in root .env"
}
$accountPoolFile = Read-EnvValue $rootEnv "NVIDIA_ACCOUNT_POOL_FILE"
$deployAccountPoolFile = ""
if ($accountPoolFile) {
  $sourceAccountPool = $accountPoolFile
  if (![System.IO.Path]::IsPathRooted($sourceAccountPool)) {
    $sourceAccountPool = Join-Path $root $sourceAccountPool
  }
  if (Test-Path $sourceAccountPool) {
    $secretDir = Join-Path $deploy "secrets"
    New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
    Copy-Item -LiteralPath $sourceAccountPool -Destination (Join-Path $secretDir "nvidia-accounts.json") -Force
    $deployAccountPoolFile = "/app/secrets/nvidia-accounts.json"
  }
}

$content = @"
# Edit PUBLIC_DOMAIN / ACME_EMAIL / ADMIN_EMAIL / ADMIN_PASSWORD before upload.
PUBLIC_DOMAIN=api.example.com
ACME_EMAIL=admin@example.com
TZ=Asia/Shanghai

ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-this-before-first-run-$(New-Token 12)

JWT_SECRET=$(New-Token 48)
TOTP_ENCRYPTION_KEY=$(New-Token 32)

POSTGRES_USER=sub2api
POSTGRES_PASSWORD=$(New-Token 32)
POSTGRES_DB=sub2api
REDIS_PASSWORD=$(New-Token 24)
BACKUP_RETENTION_DAYS=7
BACKUP_INCLUDE_CADDY_DATA=true
BACKUP_INCLUDE_REDIS_DATA=true

DOCKER_LOG_MAX_SIZE=20m
DOCKER_LOG_MAX_FILE=5

SECURITY_URL_ALLOWLIST_ENABLED=false
SECURITY_URL_ALLOWLIST_ALLOW_INSECURE_HTTP=true
SECURITY_URL_ALLOWLIST_ALLOW_PRIVATE_HOSTS=true

ADAPTER_ADMIN_TOKEN=$(New-Token 32)
ADAPTER_CLIENT_TOKEN=sk-adapter-$(New-Token 32)
ADAPTER_REQUEST_TIMEOUT_SECONDS=180
ADAPTER_KEY_COOLDOWN_SECONDS=90
ADAPTER_MAX_RETRIES=10
ADAPTER_KEY_MAX_IN_FLIGHT=1
ADAPTER_KEY_QUEUE_WAIT_SECONDS=30
ADAPTER_MAX_REQUEST_BODY_BYTES=8388608
ADAPTER_ACCESS_LOG_HEALTH=false
NVIDIA_API_KEYS=$nvidiaKeys
NVIDIA_ACCOUNT_POOL_FILE=$deployAccountPoolFile

RUN_MODE=standard
SERVER_MODE=release
OPS_ENABLED=true
DASHBOARD_AGGREGATION_ENABLED=true
GATEWAY_MAX_CONNS_PER_HOST=2048
GATEWAY_MAX_IDLE_CONNS=8192
GATEWAY_MAX_IDLE_CONNS_PER_HOST=4096
GATEWAY_SCHEDULING_FALLBACK_WAIT_TIMEOUT=30s
GATEWAY_SCHEDULING_FALLBACK_MAX_WAITING=100
"@

Set-Content -Path $targetEnv -Value $content -Encoding UTF8
Write-Host "Created $targetEnv"
Write-Host "Now edit PUBLIC_DOMAIN, ACME_EMAIL, ADMIN_EMAIL, and ADMIN_PASSWORD."
