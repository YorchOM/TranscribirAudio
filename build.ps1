param(
    [switch]$Clean,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "No se encontró .venv. Crea el entorno virtual y instala las dependencias antes de construir."
}

$py = ".\.venv\Scripts\python.exe"

# Asegurar que PyInstaller esté disponible
& $py -m pip show pyinstaller > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Instalando PyInstaller..." -ForegroundColor Cyan
    & $py -m pip install pyinstaller
}

# Comprobar assets
if (-not (Test-Path "assets\ffmpeg\ffmpeg.exe")) {
    Write-Error "Falta assets\ffmpeg\ffmpeg.exe. Copia ffmpeg.exe ahí antes de construir."
}
if (-not (Test-Path "assets\whisper_models\small.pt")) {
    Write-Error "Falta assets\whisper_models\small.pt. Copia el modelo Whisper 'small' ahí antes de construir."
}

if ($Clean) {
    Write-Host "Limpiando build/ y dist/..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

Write-Host "Empaquetando con PyInstaller..." -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm TranscribirAudio.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller terminó con error."
}

$distDir = "dist\TranscribirAudio"
if (-not (Test-Path "$distDir\TranscribirAudio.exe")) {
    Write-Error "El ejecutable no se generó en $distDir."
}

$size = (Get-ChildItem $distDir -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host ("Build OK -> $distDir ({0:N1} MB)" -f ($size/1MB)) -ForegroundColor Green

if ($Zip) {
    $zipPath = "dist\TranscribirAudio.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "Creando $zipPath ..." -ForegroundColor Cyan
    Compress-Archive -Path "$distDir\*" -DestinationPath $zipPath -CompressionLevel Optimal
    $zipSize = (Get-Item $zipPath).Length
    Write-Host ("ZIP creado -> $zipPath ({0:N1} MB)" -f ($zipSize/1MB)) -ForegroundColor Green
}
