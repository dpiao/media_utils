# Add this repo's scripts/ folder to the current user's PATH (once).
$scriptsDir = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")

$parts = $userPath -split ";" | Where-Object { $_ -ne "" }
if ($parts -contains $scriptsDir) {
    Write-Host "Already on PATH: $scriptsDir"
    exit 0
}

$newPath = ($parts + $scriptsDir) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")

# Refresh PATH in this session
$env:Path = "$env:Path;$scriptsDir"

Write-Host "Added to user PATH: $scriptsDir"
Write-Host "Open a new terminal (or log out/in) to use: mediactl, render_vr360, sync_media_to_s3"
