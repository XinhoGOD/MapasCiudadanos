$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runRoot = Join-Path $projectRoot ("work\inegi-source-" + $timestamp)
$catalogZip = Join-Path $projectRoot 'work\inegi-catun_localidad.zip'
$dcahZip = Join-Path $projectRoot 'work\inegi-dcah-hidalgo.zip'
$catalogUrl = 'https://www.inegi.org.mx/contenidos/app/ageeml/catun_localidad.zip'
$dcahUrl = 'https://www.inegi.org.mx/contenidos/productos/prod_serv/contenidos/espanol/bvinegi/productos/geografia/delimitaciones/794551163078/13_hidalgo.zip'

New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
curl.exe -L --fail --retry 2 --max-time 240 $catalogUrl -o $catalogZip
Expand-Archive -LiteralPath $catalogZip -DestinationPath $runRoot
$sourceCsv = Get-ChildItem -LiteralPath $runRoot -Recurse -Filter 'AGEEML_*.csv' | Sort-Object Name -Descending | Select-Object -First 1
if (-not $sourceCsv) {
    throw 'El ZIP del Catálogo Único no contiene un archivo AGEEML_*.csv.'
}

py -3.13 (Join-Path $projectRoot 'scripts\build_hidalgo_catalog.py') $sourceCsv.FullName (Join-Path $projectRoot 'data\geography.catalog.geojson')
curl.exe -L --fail --retry 2 --max-time 360 $dcahUrl -o $dcahZip
py -3.13 (Join-Path $projectRoot 'scripts\build_hidalgo_geography.py') --settlement-zip $dcahZip --destination (Join-Path $projectRoot 'data\geography\hidalgo')
py -3.13 (Join-Path $projectRoot 'scripts\validate_hidalgo_geography.py') --catalog-archive $catalogZip --dcah-archive $dcahZip
Write-Output 'Catálogo y polígonos oficiales de Hidalgo actualizados y validados.'
