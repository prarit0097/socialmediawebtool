param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$ngrokPath = Join-Path $PSScriptRoot "..\tools\ngrok\ngrok.exe"
$ngrokPath = [System.IO.Path]::GetFullPath($ngrokPath)
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$envPath = Join-Path $projectRoot ".env"

if (-not (Test-Path $ngrokPath)) {
    throw "ngrok.exe not found at $ngrokPath"
}

if (-not (Test-Path $envPath)) {
    throw ".env not found at $envPath"
}

$process = Start-Process -FilePath $ngrokPath -ArgumentList @("http", $Port.ToString()) -PassThru
Write-Host "Started ngrok process id $($process.Id). Waiting for public URL..."

$publicUrl = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 3
        $publicUrl = ($response.tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -First 1 -ExpandProperty public_url)
        if ($publicUrl) {
            break
        }
    } catch {
    }
}

if (-not $publicUrl) {
    throw "Could not fetch ngrok public URL from local API. Make sure authtoken is configured and ngrok started correctly."
}

$content = Get-Content $envPath -Raw
if ($content -match "(?m)^PUBLIC_APP_BASE_URL=") {
    $content = [regex]::Replace($content, "(?m)^PUBLIC_APP_BASE_URL=.*$", "PUBLIC_APP_BASE_URL=$publicUrl")
} else {
    if (-not $content.EndsWith("`n")) {
        $content += "`r`n"
    }
    $content += "PUBLIC_APP_BASE_URL=$publicUrl`r`n"
}
Set-Content -Path $envPath -Value $content -Encoding UTF8

Write-Host "PUBLIC_APP_BASE_URL updated to $publicUrl"
Write-Host "Restart Django server after this so the new URL is loaded."
Write-Host "ngrok web inspector: http://127.0.0.1:4040"
