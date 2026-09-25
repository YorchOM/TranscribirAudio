param(
    [switch]$Clean,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creando .venv e instalando dependencias..." -ForegroundColor Cyan
    py -3 -m venv .venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt
}

# Asegurar que PyInstaller esté disponible
& $py -m pip show pyinstaller > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Instalando PyInstaller..." -ForegroundColor Cyan
    & $py -m pip install pyinstaller
}

$distDir = "dist\TranscribirAudio"

# PyInstaller borra la carpeta de salida entera. Los modelos descargados y la
# configuración que haya ahí se apartan y se reponen después: si no, cada
# build obliga a volver a bajar cientos de MB.
$apartado = "dist\_conservado"
Remove-Item -Recurse -Force $apartado -ErrorAction SilentlyContinue
foreach ($cosa in "modelos", "TranscribirAudio.ini") {
    if (Test-Path "$distDir\$cosa") {
        New-Item -ItemType Directory -Force $apartado | Out-Null
        Move-Item "$distDir\$cosa" $apartado
    }
}

if ($Clean) {
    Write-Host "Limpiando build/ y dist/TranscribirAudio..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force build, $distDir -ErrorAction SilentlyContinue
}

Write-Host "Empaquetando con PyInstaller..." -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm TranscribirAudio.spec
$fallo = $LASTEXITCODE

# Reponer lo apartado pase lo que pase
if (Test-Path $apartado) {
    New-Item -ItemType Directory -Force $distDir | Out-Null
    Get-ChildItem $apartado | Move-Item -Destination $distDir -Force
    Remove-Item -Recurse -Force $apartado
}
if ($fallo -ne 0) {
    Write-Error "PyInstaller terminó con error."
}

foreach ($exe in @("TranscribirAudio.exe")) {
    if (-not (Test-Path "$distDir\$exe")) {
        Write-Error "No se generó $distDir\$exe."
    }
}

# Configuración de fábrica, generada desde la plantilla de gestor_modelos.py.
# No se copia el TranscribirAudio.ini del proyecto: ése es el de quien
# desarrolla (la app lo cambia al usarla) y no debe viajar en el ZIP.
$iniFabrica = "build\TranscribirAudio.ini"
& $py -c "import gestor_modelos as gm; open(r'$iniFabrica', 'w', encoding='utf-8').write(gm.PLANTILLA_INI.format(modelo=gm.MODELO_POR_DEFECTO, idioma='', carpeta_salida=''))"
# Va junto al .exe (no dentro de _internal) para poder editarla. Si ya había
# una (la conservada), se respeta.
if (-not (Test-Path "$distDir\TranscribirAudio.ini")) {
    Copy-Item $iniFabrica $distDir
}

$size = (Get-ChildItem $distDir -Recurse -Exclude modelos |
    Where-Object { $_.FullName -notlike "*\modelos\*" } |
    Measure-Object -Property Length -Sum).Sum
Write-Host ("Build OK -> $distDir ({0:N1} MB sin contar modelos)" -f ($size/1MB)) -ForegroundColor Green

if ($Zip) {
    # El ZIP es para repartir: sin modelos (cada uno se baja el suyo la primera
    # vez) y con la configuración de fábrica, no la de esta máquina.
    $zipPath = "dist\TranscribirAudio.zip"
    $staging = "dist\_zip\TranscribirAudio"
    Remove-Item -Recurse -Force "dist\_zip", $zipPath -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $staging | Out-Null
    Get-ChildItem $distDir -Exclude modelos, TranscribirAudio.ini | Copy-Item -Destination $staging -Recurse
    Copy-Item $iniFabrica $staging
    Write-Host "Creando $zipPath ..." -ForegroundColor Cyan
    Compress-Archive -Path $staging -DestinationPath $zipPath -CompressionLevel Optimal
    Remove-Item -Recurse -Force "dist\_zip"
    $zipSize = (Get-Item $zipPath).Length
    Write-Host ("ZIP creado -> $zipPath ({0:N1} MB)" -f ($zipSize/1MB)) -ForegroundColor Green
}
