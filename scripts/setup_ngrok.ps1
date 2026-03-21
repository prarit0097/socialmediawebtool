param(
    [Parameter(Mandatory = $true)]
    [string]$AuthToken
)

$ErrorActionPreference = "Stop"

$ngrokPath = Join-Path $PSScriptRoot "..\tools\ngrok\ngrok.exe"
$ngrokPath = [System.IO.Path]::GetFullPath($ngrokPath)

if (-not (Test-Path $ngrokPath)) {
    throw "ngrok.exe not found at $ngrokPath"
}

& $ngrokPath config add-authtoken $AuthToken
Write-Host "ngrok authtoken configured successfully."
