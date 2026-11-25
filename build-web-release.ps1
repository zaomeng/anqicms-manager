param(
    [string]$ReleaseDate = (Get-Date -Format "yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root ".omx\build-venv"
$python = Join-Path $venv "Scripts\python.exe"
$pyinstaller = Join-Path $venv "Scripts\pyinstaller.exe"
$workDir = Join-Path $root ".omx\build-web-release"
$candidateDir = Join-Path $root ".omx\dist-web-release"
$specFile = Join-Path $root "anqicms_web.spec"
$staticDir = Join-Path $root "web\static"
$exeName = "anqicms站群控制台.exe"
$zipName = "anqicms站群控制台.zip"

if (-not (Test-Path -LiteralPath $specFile)) {
    throw "PyInstaller spec file not found: $specFile"
}
if (-not (Test-Path -LiteralPath $staticDir)) {
    throw "Web static assets directory not found: $staticDir"
}

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create build environment: $LASTEXITCODE" }
}
& $python -m pip install --disable-pip-version-check --requirement (Join-Path $root "requirements-build.lock")
if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies: $LASTEXITCODE" }
& $python -c "import requests, urllib3; assert requests.__version__ == '2.33.0'; assert urllib3.__version__ == '2.7.0'"
if ($LASTEXITCODE -ne 0) { throw "Build dependency version check failed: $LASTEXITCODE" }
& $pyinstaller --clean --noconfirm --workpath $workDir --distpath $candidateDir $specFile
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed: $LASTEXITCODE" }

$candidate = Join-Path $candidateDir $exeName
if (-not (Test-Path -LiteralPath $candidate)) {
    throw "Expected output executable not found: $candidate"
}

$distDir = Join-Path $root "dist"
$releaseDir = Join-Path $root "release\$ReleaseDate"
New-Item -ItemType Directory -Force -Path $distDir, $releaseDir | Out-Null
Copy-Item -LiteralPath $candidate -Destination (Join-Path $distDir $exeName) -Force
Copy-Item -LiteralPath $candidate -Destination (Join-Path $releaseDir $exeName) -Force

$releaseExe = Join-Path $releaseDir $exeName
$releaseZip = Join-Path $releaseDir $zipName
Compress-Archive -LiteralPath $releaseExe -DestinationPath $releaseZip -Force
$checksumLines = @($releaseExe, $releaseZip) | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash.ToLowerInvariant()
    "$hash *$(Split-Path -Leaf $_)"
}
Set-Content -LiteralPath (Join-Path $releaseDir "SHA256SUMS-web.txt") -Value $checksumLines -Encoding utf8
