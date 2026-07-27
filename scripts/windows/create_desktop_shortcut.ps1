# Create a Desktop shortcut for mediactl.
$scriptsDir = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$cmdPath = Join-Path $scriptsDir "mediactl.cmd"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "mediactl.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $cmdPath
$shortcut.WorkingDirectory = $scriptsDir
$shortcut.Description = "Start mediactl (background media supervisor)"
$shortcut.WindowStyle = 7  # Minimized
$shortcut.Save()

Write-Host "Created: $shortcutPath"
