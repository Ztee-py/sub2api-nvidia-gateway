$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$tokenLine = Get-Content .\.env | Where-Object { $_ -match '^SUB2API_ACCESS_TOKEN=' } | Select-Object -First 1
$token = $tokenLine -replace '^SUB2API_ACCESS_TOKEN=', ''

$headers = @{
  Authorization = "Bearer $token"
  "Content-Type" = "application/json"
}

$body = @{
  model = "deepseekv4-pro"
  messages = @(
    @{ role = "user"; content = "用一句话介绍你自己。" }
  )
  temperature = 0.3
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/chat/completions" -Method Post -Headers $headers -Body $body
