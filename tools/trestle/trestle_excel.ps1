<#
  trestle_excel.ps1
  Lee un Excel (.xlsx) fila por fila, consulta cada telefono en Trestle Phone Intel 3.0
  desde ESTA maquina y guarda un CSV con las columnas originales + los datos de Trestle.

  Al terminar SIN errores mueve a <carpeta del programa>\completed\<fecha>_<nombre>\ :
    - el Excel procesado
    - el CSV de resultados
    - el JSONL con las respuestas completas
  Si hubo errores, los archivos se quedan en la carpeta del programa: vuelve a ejecutarlo
  y los telefonos que ya respondieron bien NO se vuelven a consultar ni a cobrar.

  Clave: variable de entorno TRESTLE_APIKEY (si no existe, la pide y la guarda).
#>
[CmdletBinding()]
param(
    [string]$InputXlsx    = "",    # vacio = ventana para elegir el Excel
    [string]$Sheet        = "",    # vacio = primera hoja
    [string]$PhoneColumn  = "",    # vacio = detecta automaticamente
    [double]$DelaySeconds = 1,     # pausa entre consultas
    [int]$MaxRows         = 0,     # 0 = todas las filas
    [int]$MaxLeads        = 0,     # 0 = todos; N = solo los primeros N leads (owner_uuid)
    [switch]$Litigator,            # agrega el add-on litigator_checks (costo extra)
    [switch]$ResetKey              # borra la clave guardada y pide una nueva
)

$ErrorActionPreference = "Stop"
$Base      = $PSScriptRoot
$Completed = Join-Path $Base "completed"

function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ============================== CLAVE ==============================
if ($ResetKey) {
    [Environment]::SetEnvironmentVariable("TRESTLE_APIKEY", $null, "User")
    $env:TRESTLE_APIKEY = $null
    Write-Host "Clave anterior borrada." -ForegroundColor Yellow
}
$apiKey = $env:TRESTLE_APIKEY
if ([string]::IsNullOrWhiteSpace($apiKey)) { $apiKey = [Environment]::GetEnvironmentVariable("TRESTLE_APIKEY", "User") }
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    Write-Host ""
    Write-Host "No hay clave de Trestle guardada en este equipo." -ForegroundColor Cyan
    Write-Host "Pegala con CLIC DERECHO y pulsa Enter (no se vera mientras la pegas):"
    $sec = Read-Host "Clave Trestle" -AsSecureString
    $apiKey = ((New-Object System.Net.NetworkCredential("", $sec)).Password) -replace '[\x00-\x20\x7F]', ''
    if ($apiKey -notmatch '^[A-Za-z0-9]{20,}$') { Fail "la clave no tiene el formato esperado (largo: $($apiKey.Length)). Pegala con clic derecho." }
    [Environment]::SetEnvironmentVariable("TRESTLE_APIKEY", $apiKey, "User")
    Write-Host "Clave guardada en tu usuario de Windows. No te la volvera a pedir." -ForegroundColor Green
}
$apiKey = $apiKey -replace '[\x00-\x20\x7F]', ''
if ($apiKey -notmatch '^[A-Za-z0-9]{20,}$') { Fail "la clave guardada no tiene el formato esperado. Ejecuta cambiar_clave_trestle.bat" }

# ============================== EXCEL DE ENTRADA ==============================
if (-not $InputXlsx) {
    Add-Type -AssemblyName System.Windows.Forms
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title            = "Elige el Excel con los telefonos a revisar en Trestle"
    $dlg.Filter           = "Libro de Excel (*.xlsx)|*.xlsx"
    $dlg.InitialDirectory = $Base
    if ($dlg.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { Fail "no elegiste ningun archivo." }
    $InputXlsx = $dlg.FileName
}
if (-not (Test-Path $InputXlsx)) { Fail "no existe $InputXlsx" }
if ($InputXlsx -notmatch '\.xlsx$') { Fail "el archivo debe ser .xlsx (en Excel: Guardar como > Libro de Excel)." }
$InputXlsx = (Resolve-Path $InputXlsx).Path
Write-Host "Archivo: $InputXlsx"

# ---- Lector de .xlsx (no necesita Excel instalado) ----
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-ZipXml($zip, [string]$path) {
    $entry = $zip.GetEntry($path)
    if (-not $entry) { return $null }
    $sr = New-Object System.IO.StreamReader($entry.Open(), [Text.Encoding]::UTF8)
    try { $x = New-Object System.Xml.XmlDocument; $x.LoadXml($sr.ReadToEnd()); return $x } finally { $sr.Dispose() }
}

function Get-ColIndex([string]$ref) {
    $n = 0
    foreach ($ch in (($ref -replace '[0-9]', '').ToUpper()).ToCharArray()) { $n = $n * 26 + ([int][char]$ch - 64) }
    return $n - 1
}

function Read-Xlsx([string]$path, [string]$sheetName) {
    $fs  = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Read)
    try {
        $wb     = Get-ZipXml $zip "xl/workbook.xml"
        $rels   = Get-ZipXml $zip "xl/_rels/workbook.xml.rels"
        $sheets = @($wb.SelectNodes("//*[local-name()='sheet']"))
        if ($sheets.Count -eq 0) { throw "El Excel no tiene hojas." }
        $target = $sheets[0]
        if ($sheetName) {
            $target = $sheets | Where-Object { $_.GetAttribute("name") -eq $sheetName } | Select-Object -First 1
            if (-not $target) { throw "No existe la hoja '$sheetName'." }
        }
        Write-Host "Hoja: $($target.GetAttribute('name'))"
        $rid = $target.GetAttribute("id", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
        $rel = $rels.SelectNodes("//*[local-name()='Relationship']") | Where-Object { $_.GetAttribute("Id") -eq $rid } | Select-Object -First 1
        $sheetPath = $rel.GetAttribute("Target")
        if ($sheetPath.StartsWith("/")) { $sheetPath = $sheetPath.TrimStart("/") } else { $sheetPath = "xl/" + $sheetPath }

        $shared = New-Object System.Collections.Generic.List[string]
        $ss = Get-ZipXml $zip "xl/sharedStrings.xml"
        if ($ss) {
            foreach ($si in $ss.SelectNodes("//*[local-name()='si']")) {
                $shared.Add((($si.SelectNodes(".//*[local-name()='t']") | ForEach-Object { $_.InnerText }) -join ""))
            }
        }

        $sx   = Get-ZipXml $zip $sheetPath
        $rows = New-Object System.Collections.Generic.List[object]
        foreach ($row in $sx.SelectNodes("//*[local-name()='sheetData']/*[local-name()='row']")) {
            $cells = @{}; $max = -1; $auto = 0
            foreach ($c in $row.SelectNodes("*[local-name()='c']")) {
                $ref = $c.GetAttribute("r")
                $idx = if ($ref) { Get-ColIndex $ref } else { $auto }
                $auto = $idx + 1
                $t = $c.GetAttribute("t")
                $v = $c.SelectSingleNode("*[local-name()='v']")
                $val = ""
                if ($t -eq "s" -and $v)     { $val = $shared[[int]$v.InnerText] }
                elseif ($t -eq "inlineStr") { $val = ($c.SelectNodes(".//*[local-name()='t']") | ForEach-Object { $_.InnerText }) -join "" }
                elseif ($v)                 { $val = $v.InnerText }
                if ($t -notin @("s", "inlineStr", "str") -and $val -match '^[0-9.]+E\+?[0-9]+$') {
                    $inv = [Globalization.CultureInfo]::InvariantCulture
                    $val = ([decimal]::Parse($val, [Globalization.NumberStyles]::Float, $inv)).ToString("0", $inv)
                }
                $cells[$idx] = $val
                if ($idx -gt $max) { $max = $idx }
            }
            $rows.Add(@(for ($j = 0; $j -le $max; $j++) { if ($cells.ContainsKey($j)) { [string]$cells[$j] } else { "" } }))
        }
        return ,$rows
    } finally { $zip.Dispose(); $fs.Dispose() }
}

$raw = Read-Xlsx $InputXlsx $Sheet
$h = 0
while ($h -lt $raw.Count -and (($raw[$h] -join "").Trim() -eq "")) { $h++ }
if ($h -ge $raw.Count) { Fail "la hoja esta vacia." }

# Encabezados (sin repetidos)
$headers = @(); $usedNames = @{}
for ($j = 0; $j -lt $raw[$h].Count; $j++) {
    $name = "$($raw[$h][$j])".Trim()
    if (-not $name) { $name = "col_$($j + 1)" }
    $baseName = $name; $k = 2
    while ($usedNames.ContainsKey($name)) { $name = "${baseName}_$k"; $k++ }
    $usedNames[$name] = $true
    $headers += $name
}

# Columna de telefono
$phoneIdx = -1
if ($PhoneColumn) {
    $phoneIdx = [Array]::IndexOf($headers, $PhoneColumn)
    if ($phoneIdx -lt 0) { Fail "no hay columna '$PhoneColumn'. Columnas: $($headers -join ', ')" }
} else {
    for ($j = 0; $j -lt $headers.Count; $j++) {
        if ($headers[$j] -match '(?i)^(phone|phone[ _]?number|tel[e\u00e9]fono|tel|celular|cell|mobile|m[o\u00f3]vil)$') { $phoneIdx = $j; break }
    }
    if ($phoneIdx -lt 0) {
        for ($j = 0; $j -lt $headers.Count; $j++) {
            if ($headers[$j] -match '(?i)phone|tel[e\u00e9]fono|celular|mobile' -and $headers[$j] -notmatch '(?i)json|type|tipo|status|estado') { $phoneIdx = $j; break }
        }
    }
    if ($phoneIdx -lt 0) { Fail "no encontre la columna de telefonos. Usa -PhoneColumn. Columnas: $($headers -join ', ')" }
}
Write-Host "Columna de telefono: $($headers[$phoneIdx])"

# Filas de datos (con limite opcional por filas o por leads)
$leadIdx   = [Array]::IndexOf($headers, "owner_uuid")
$leadsSeen = New-Object System.Collections.Generic.List[string]
if ($MaxLeads -gt 0 -and $leadIdx -lt 0) { Write-Host "AVISO: no hay columna owner_uuid; se ignora el limite de leads." -ForegroundColor Yellow }
$dataRows = @()
for ($r = $h + 1; $r -lt $raw.Count; $r++) {
    if ((($raw[$r] -join "").Trim()) -eq "") { continue }
    if ($MaxLeads -gt 0 -and $leadIdx -ge 0) {
        $lead = if ($leadIdx -lt $raw[$r].Count) { "$($raw[$r][$leadIdx])" } else { "" }
        if (-not $leadsSeen.Contains($lead)) {
            if ($leadsSeen.Count -ge $MaxLeads) { continue }
            $leadsSeen.Add($lead)
        }
    }
    $dataRows += ,@($raw[$r])
    if ($MaxRows -gt 0 -and $dataRows.Count -ge $MaxRows) { break }
}
if ($MaxLeads -gt 0 -and $leadIdx -ge 0) { Write-Host "Leads a procesar: $($leadsSeen.Count)" }
Write-Host "Filas a procesar: $($dataRows.Count)"
if ($Litigator) { Write-Host "Add-on de litigantes ACTIVADO (litigator_checks, costo extra por consulta)" -ForegroundColor Cyan }

# ============================== ARCHIVOS DE SALIDA ==============================
# Mientras corre, se escriben en la carpeta del programa (permite retomar si se corta).
$stem     = [IO.Path]::GetFileNameWithoutExtension($InputXlsx)
$suffix   = if ($Litigator) { "_trestle_litigator" } else { "_trestle" }
$OutCsv   = Join-Path $Base ($stem + $suffix + ".csv")
$OutJsonl = Join-Path $Base ($stem + $suffix + ".jsonl")

# Respuestas ya obtenidas en una corrida anterior incompleta (no se vuelven a cobrar)
$cache = @{}
if (Test-Path $OutJsonl) {
    foreach ($line in [IO.File]::ReadAllLines($OutJsonl, [Text.Encoding]::UTF8)) {
        if (-not $line.Trim()) { continue }
        try { $o = $line | ConvertFrom-Json; if ($o.http_status -eq 200) { $cache[$o.phone] = $o } } catch { }
    }
    if ($cache.Count) { Write-Host "Retomando: $($cache.Count) telefonos ya consultados en una corrida anterior." -ForegroundColor Cyan }
}

# ============================== LLAMADA A TRESTLE ==============================
$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
if (-not $curl) { Fail "no se encontro curl.exe" }
$headerFile = [IO.Path]::GetTempFileName()
$bodyFile   = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($headerFile, "x-api-key: $apiKey`r`naccept: application/json`r`n", [Text.Encoding]::ASCII)

function Invoke-Trestle([string]$phone) {
    $url = "https://api.trestleiq.com/3.0/phone_intel?phone=$phone"
    if ($Litigator) { $url += "&add_ons=litigator_checks" }
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        [IO.File]::WriteAllText($bodyFile, "")
        $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $res  = & $curl -s -S --connect-timeout 20 --max-time 60 -H "@$headerFile" -o $bodyFile -w "%{http_code}" $url 2>&1 | ForEach-Object { "$_" }
        $exit = $LASTEXITCODE
        $ErrorActionPreference = $prev
        $res  = ($res -join " ").Trim()
        $body = [IO.File]::ReadAllText($bodyFile, [Text.Encoding]::UTF8)
        if ($exit -ne 0) {
            if ($attempt -lt 2) { Start-Sleep -Seconds 5; continue }
            return [pscustomobject]@{ http_status = 0; body = $body; error = "conexion fallida (curl $exit): $res" }
        }
        $status = [int]($res -replace '\D', '')
        if ((($status -eq 429) -or ($status -ge 500)) -and $attempt -lt 2) { Start-Sleep -Seconds 5; continue }
        return [pscustomobject]@{ http_status = $status; body = $body; error = $null }
    }
}

function To-Cell($v) {
    if ($null -eq $v) { return "" }
    if ($v -is [string] -or $v -is [ValueType]) { return $v }
    return ($v | ConvertTo-Json -Depth 10 -Compress)
}

# ============================== PROCESO ==============================
$results = New-Object System.Collections.Generic.List[object]
$stats   = @{ ok = 0; reutilizado = 0; error = 0; invalido = 0 }
$aborted = $false
$n = 0
try {
    foreach ($row in $dataRows) {
        $n++
        $out = [ordered]@{}
        for ($c = 0; $c -lt $headers.Count; $c++) { $out[$headers[$c]] = if ($c -lt $row.Count) { $row[$c] } else { "" } }

        $orig   = if ($phoneIdx -lt $row.Count) { "$($row[$phoneIdx])" } else { "" }
        $digits = $orig -replace '\D', ''
        if ($digits.Length -eq 11 -and $digits.StartsWith('1')) { $digits = $digits.Substring(1) }

        if ($digits.Length -ne 10) {
            $out["trestle_http_status"] = ""
            $out["trestle_estado"] = if ($digits) { "numero invalido ($($digits.Length) digitos), no consultado" } else { "sin telefono, no consultado" }
            $stats.invalido++
            Write-Host ("{0}/{1}  '{2}'  -> {3}" -f $n, $dataRows.Count, $orig, $out["trestle_estado"]) -ForegroundColor DarkGray
            $results.Add([pscustomobject]$out); continue
        }

        $fromCache = $cache.ContainsKey($digits)
        if ($fromCache) {
            $resp = $cache[$digits]
        } else {
            $call   = Invoke-Trestle $digits
            $parsed = $null; try { $parsed = $call.body | ConvertFrom-Json } catch { }
            $resp   = [pscustomobject]@{ phone = $digits; http_status = $call.http_status; response = $(if ($parsed) { $parsed } else { $call.body }); error = $call.error }
            [IO.File]::AppendAllText($OutJsonl, (($resp | ConvertTo-Json -Depth 20 -Compress) + "`n"), (New-Object Text.UTF8Encoding($false)))
            if ($resp.http_status -eq 200) { $cache[$digits] = $resp }
        }

        $out["trestle_http_status"] = $resp.http_status
        if ($resp.http_status -eq 200 -and $resp.response -isnot [string]) {
            $t = $resp.response
            foreach ($p in $t.PSObject.Properties) { $out["trestle_$($p.Name)"] = To-Cell $p.Value }
            $litTxt = ""
            if ($Litigator) {
                $lit = $null
                if ($t.add_ons -and $t.add_ons.litigator_checks) { $lit = $t.add_ons.litigator_checks."phone.is_litigator_risk" }
                $out["trestle_is_litigator_risk"] = if ($null -eq $lit) { "sin dato" } else { $lit }
                $litTxt = "  litigante=$($out['trestle_is_litigator_risk'])"
            }
            $out["trestle_estado"] = if ($fromCache) { "ok (ya consultado)" } else { "ok" }
            if ($fromCache) { $stats.reutilizado++ } else { $stats.ok++ }
            Write-Host ("{0}/{1}  {2}  OK  valido={3} score={4} tipo={5} operador={6}{7}" -f $n, $dataRows.Count, $digits, $t.is_valid, $t.activity_score, $t.line_type, $t.carrier, $litTxt) -ForegroundColor Green
            $results.Add([pscustomobject]$out)
        } else {
            $msg = $resp.error
            if (-not $msg -and $resp.response -isnot [string] -and $resp.response.errorCode) { $msg = "$($resp.response.errorCode) $($resp.response.message)" }
            if (-not $msg) { $msg = "$($resp.response)" }
            $out["trestle_estado"] = "error: $msg"
            $stats.error++
            Write-Host ("{0}/{1}  {2}  HTTP {3}  {4}" -f $n, $dataRows.Count, $digits, $resp.http_status, $msg) -ForegroundColor Yellow
            $results.Add([pscustomobject]$out)
            if ($resp.http_status -eq 403) {
                Write-Host "Trestle rechazo la clave (403). Me detengo. Si cambiaste de clave, ejecuta cambiar_clave_trestle.bat" -ForegroundColor Red
                $aborted = $true; break
            }
        }
        if (-not $fromCache -and $n -lt $dataRows.Count -and $DelaySeconds -gt 0) { Start-Sleep -Milliseconds ([int]($DelaySeconds * 1000)) }
    }
}
finally {
    Remove-Item $headerFile, $bodyFile -Force -ErrorAction SilentlyContinue
}

# ============================== GUARDAR CSV ==============================
$cols = New-Object System.Collections.Generic.List[string]
foreach ($o in $results) { foreach ($nm in $o.PSObject.Properties.Name) { if (-not $cols.Contains($nm)) { $cols.Add($nm) } } }
$enc = if ($PSVersionTable.PSVersion.Major -ge 6) { "utf8BOM" } else { "UTF8" }
$results | Select-Object -Property $cols | Export-Csv -Path $OutCsv -NoTypeInformation -Encoding $enc -UseCulture

Write-Host ""
Write-Host "Resumen -> Consultados OK: $($stats.ok)  Ya consultados: $($stats.reutilizado)  Errores: $($stats.error)  No consultados: $($stats.invalido)"

# ============================== MOVER A completed ==============================
if ($aborted -or $stats.error -gt 0) {
    Write-Host ""
    Write-Host "Hubo errores: los archivos se quedan en $Base para que puedas volver a ejecutarlo." -ForegroundColor Yellow
    Write-Host "Los telefonos que ya respondieron bien NO se volveran a consultar."
    Write-Host "Resultado parcial: $OutCsv"
    exit 1
}

$runDir = Join-Path $Completed ((Get-Date -Format "yyyy-MM-dd_HHmmss") + "_" + $stem + $suffix)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
Move-Item -LiteralPath $OutCsv   -Destination $runDir -Force
Move-Item -LiteralPath $OutJsonl -Destination $runDir -Force -ErrorAction SilentlyContinue
try {
    Move-Item -LiteralPath $InputXlsx -Destination $runDir -Force
    $xlsxMsg = "movido"
} catch {
    Copy-Item -LiteralPath $InputXlsx -Destination $runDir -Force
    $xlsxMsg = "COPIADO (no se pudo mover; probablemente esta abierto en Excel. Cierralo y borralo a mano)"
}

Write-Host ""
Write-Host "Listo. Todo quedo en:" -ForegroundColor Green
Write-Host "  $runDir" -ForegroundColor Green
Write-Host "    - $stem$suffix.csv    (resultado, abrir en Excel)"
Write-Host "    - $stem$suffix.jsonl  (respuestas completas de Trestle)"
Write-Host "    - $([IO.Path]::GetFileName($InputXlsx))  (Excel original, $xlsxMsg)"