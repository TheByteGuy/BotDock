# Makes dist/botdock-app.zip: just the files the NAS needs to build the image.
# Unzip it into <share>/apps/botdock/app on the NAS.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$stage = Join-Path $root "dist/app"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force (Join-Path $stage "deploy") | Out-Null
Copy-Item (Join-Path $root "Dockerfile"), (Join-Path $root ".dockerignore"), (Join-Path $root "requirements.txt") $stage
Copy-Item (Join-Path $root "deploy/entrypoint.sh") (Join-Path $stage "deploy")
robocopy (Join-Path $root "botdock") (Join-Path $stage "botdock") /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS | Out-Null
$zip = Join-Path $root "dist/botdock-app.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
# Windows' own tar writes forward-slash paths, so the zip also unpacks correctly on Linux.
$tar = Join-Path $env:SystemRoot "System32/tar.exe"
& $tar -a -c -f $zip -C $stage .
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
Remove-Item -Recurse -Force $stage
Write-Host "Wrote $zip"
