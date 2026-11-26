param([Parameter(Mandatory=$true)][string]$Version)
# Uploads catalog.json (made by the build) to the papro repository on GitHub, replacing the one there.
$ErrorActionPreference = "Stop"
$file = Join-Path $PSScriptRoot "catalog.json"
$bytes = [IO.File]::ReadAllBytes($file)
$null = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json   # must be valid JSON
$sha = gh api repos/sjames1066/papro/contents/catalog.json --jq .sha
if ($LASTEXITCODE -ne 0) { throw "Could not read catalog.json on GitHub" }
$body = @{ message = "Catalog: PAPro $Version"; content = [Convert]::ToBase64String($bytes); sha = $sha; branch = "main" } | ConvertTo-Json
$body | gh api -X PUT repos/sjames1066/papro/contents/catalog.json --input - | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not upload catalog.json" }
Write-Host "catalog.json published for PAPro $Version"
