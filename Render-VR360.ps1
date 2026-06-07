#Requires -Version 5.1
<#
.SYNOPSIS
    Assembles VAM-rendered PNG/JPG frame sequences into a 360 monoscopic VR video.

.DESCRIPTION
    Combines an image sequence and WAV audio from the VAM Video Renderer plugin
    into an H.265 MP4 with spherical (equirectangular) metadata injected.

    Encoder priority: hevc_nvenc (GPU, faster) with automatic fallback to libx265 (CPU).
    Metadata is injected via exiftool if available, otherwise skipped with a warning.

.PARAMETER SourceFolder
    Folder containing the rendered frame sequence and WAV file.
    Defaults to the current directory.

.PARAMETER Framerate
    Playback framerate of the output video. Defaults to 60.

.PARAMETER Crf
    Quality level for libx265 fallback (0=lossless, 51=worst). Defaults to 20.

.PARAMETER Cq
    Quality level for hevc_nvenc (0=best, 51=worst). Defaults to 20.

.PARAMETER Resolution
    Output resolution as WxH (e.g. 3840x1920). Defaults to source resolution (no scaling).

.PARAMETER StereoMode
    Spherical stereo mode tag: mono, left-right, top-bottom. Defaults to mono.

.PARAMETER OutputName
    Base name for the output file (without extension). Defaults to source folder name.

.PARAMETER ExifToolPath
    Path to exiftool.exe. Searched in PATH if not specified.

.EXAMPLE
    .\Render-VR360.ps1 -SourceFolder "C:\Games\Vam\Saves\VR_Renders\20260607-002259"

.EXAMPLE
    .\Render-VR360.ps1 -SourceFolder ".\my_render" -Framerate 30 -Resolution 3840x1920
#>
[CmdletBinding()]
param(
    [string]$SourceFolder = ".",
    [int]$Framerate = 60,
    [int]$Crf = 20,
    [int]$Cq = 20,
    [string]$Resolution = "",
    [ValidateSet("mono", "left-right", "top-bottom")]
    [string]$StereoMode = "mono",
    [string]$OutputName = "",
    [string]$ExifToolPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Resolve paths ────────────────────────────────────────────────────────────
$SourceFolder = (Resolve-Path $SourceFolder).Path
$folderName   = Split-Path $SourceFolder -Leaf

if (-not $OutputName) { $OutputName = $folderName }

$outputDir = Join-Path $SourceFolder "rendered"
New-Item -ItemType Directory -Force $outputDir | Out-Null

$suffix     = "360$StereoMode"
$outputFile = Join-Path $outputDir "${OutputName}_${suffix}.mp4"

Write-Host "`n==> VAM VR360 Renderer" -ForegroundColor Cyan
Write-Host "    Source : $SourceFolder"
Write-Host "    Output : $outputFile"

# ── Find frame sequence ──────────────────────────────────────────────────────
$pngFrames = @(Get-ChildItem $SourceFolder -Filter "*_??????.png" | Sort-Object Name)
$jpgFrames = @(Get-ChildItem $SourceFolder -Filter "*_??????.jpg" | Sort-Object Name)

if ($pngFrames.Count -ge $jpgFrames.Count -and $pngFrames.Count -gt 0) {
    $frames     = $pngFrames
    $ext        = "png"
} elseif ($jpgFrames.Count -gt 0) {
    $frames     = $jpgFrames
    $ext        = "jpg"
} else {
    Write-Error "No PNG or JPG frame sequence found in: $SourceFolder"
    exit 1
}

# Derive pattern from first frame name: basename_000000.ext -> basename_%06d.ext
$baseName    = $frames[0].BaseName -replace '_\d{6}$', ''
$pattern     = "${baseName}_%06d.${ext}"
$inputFrames = Join-Path $SourceFolder $pattern

Write-Host "    Frames : $($frames.Count) x .$ext  ($pattern)"

# ── Find audio ───────────────────────────────────────────────────────────────
$wavFile = Get-ChildItem $SourceFolder -Filter "*.wav" | Sort-Object Name | Select-Object -Last 1
if ($wavFile) {
    Write-Host "    Audio  : $($wavFile.Name)"
    $audioArgs = @("-i", $wavFile.FullName, "-c:a", "aac", "-b:a", "192k")
} else {
    Write-Host "    Audio  : none found, encoding video-only" -ForegroundColor Yellow
    $audioArgs = @("-an")
}

# ── Build video filter chain ─────────────────────────────────────────────────
$vfFilters = @("format=yuv420p")
if ($Resolution -match '^\d+x\d+$') {
    $vfFilters = @("scale=$Resolution") + $vfFilters
    Write-Host "    Scale  : $Resolution"
}
$vfArg = $vfFilters -join ","

# ── Probe for exiftool ───────────────────────────────────────────────────────
if (-not $ExifToolPath) {
    $found = Get-Command exiftool -ErrorAction SilentlyContinue
    if ($found) { $ExifToolPath = $found.Source }
}
if (-not $ExifToolPath -or -not (Test-Path $ExifToolPath)) {
    Write-Warning "exiftool not found - spherical metadata will NOT be injected. Install from https://exiftool.org or pass -ExifToolPath."
    $ExifToolPath = $null
}

# ── Try hevc_nvenc first, fall back to libx265 ───────────────────────────────
function Invoke-FFmpeg {
    param([string[]]$FfmpegArgs)
    $ErrorActionPreference = "Continue"
    & ffmpeg @FfmpegArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
    return $LASTEXITCODE
}

$commonArgs = @(
    "-y",
    "-framerate", $Framerate,
    "-i", $inputFrames
) + $audioArgs + @(
    "-vf", $vfArg,
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart"
)

Write-Host "`n--> Trying hevc_nvenc (GPU)..." -ForegroundColor Green
$nvencArgs = $commonArgs + @(
    "-c:v", "hevc_nvenc",
    "-preset", "p4",
    "-rc", "vbr",
    "-cq", $Cq,
    $outputFile
)

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$exitCode = Invoke-FFmpeg $nvencArgs
$sw.Stop()

if ($exitCode -ne 0 -or -not (Test-Path $outputFile) -or (Get-Item $outputFile).Length -eq 0) {
    Write-Host "    hevc_nvenc failed, falling back to libx265 (CPU)..." -ForegroundColor Yellow
    Remove-Item $outputFile -ErrorAction SilentlyContinue

    $x265Args = $commonArgs + @(
        "-c:v", "libx265",
        "-tune:v", "fastdecode",
        "-level:v", "6.2",
        "-crf", [string]$Crf,
        $outputFile
    )

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $exitCode = Invoke-FFmpeg $x265Args
    $sw.Stop()

    if ($exitCode -ne 0) {
        Write-Error "Encoding failed with both hevc_nvenc and libx265."
        exit 1
    }
    $encoderUsed = "libx265 (CPU)"
} else {
    $encoderUsed = "hevc_nvenc (GPU)"
}

$elapsed = $sw.Elapsed
$fps     = [math]::Round($frames.Count / $sw.Elapsed.TotalSeconds, 1)
$sizeMb  = [math]::Round((Get-Item $outputFile).Length / 1MB, 2)
Write-Host "    Encoder: $encoderUsed  |  $fps encode-fps  |  $([math]::Round($elapsed.TotalSeconds,1))s  |  ${sizeMb} MB" -ForegroundColor Green

# ── Inject spherical metadata ─────────────────────────────────────────────────
if ($ExifToolPath) {
    Write-Host "`n--> Injecting 360 spherical metadata..." -ForegroundColor Green
    & $ExifToolPath `
        "-XMP-GSpherical:Spherical=true" `
        "-XMP-GSpherical:Stitched=true" `
        "-XMP-GSpherical:ProjectionType=equirectangular" `
        "-XMP-GSpherical:StereoMode=$StereoMode" `
        "-overwrite_original" `
        $outputFile | Out-Null
    Write-Host "    Metadata injected: equirectangular / $StereoMode"
}

Write-Host "`n==> Done: $outputFile`n" -ForegroundColor Cyan
