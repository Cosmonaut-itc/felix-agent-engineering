param([string]$StateDir, [string]$ConfigPath, [switch]$NoPersist, $VentanaMs = 1000)

$script:DesktopDefaultsPath = Join-Path $PSScriptRoot 'umbrales.json'

function ConvertTo-DesktopMap {
    param($Value)
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $map = @{}
        foreach ($p in $Value.PSObject.Properties) { $map[$p.Name] = ConvertTo-DesktopMap $p.Value }
        return $map
    }
    return ,$Value
}

function Test-DesktopNumber {
    param($Value)
    return (($Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64] -or $Value -is [decimal] -or
        $Value -is [single] -or $Value -is [double]) -and
        -not [double]::IsNaN([double]$Value) -and -not [double]::IsInfinity([double]$Value))
}

function Read-DesktopConfig {
    param([string]$ConfigPath)
    $config = ConvertTo-DesktopMap (Get-Content -LiteralPath $script:DesktopDefaultsPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop)
    if ($ConfigPath) {
        $overrides = ConvertTo-DesktopMap (Get-Content -LiteralPath $ConfigPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop)
        if ($overrides -isnot [hashtable]) { throw 'Config debe ser un objeto JSON.' }
        foreach ($key in $overrides.Keys) {
            if (-not $config.ContainsKey($key)) { throw "Clave desconocida: $key" }
            if ($config[$key] -is [hashtable]) {
                if ($overrides[$key] -isnot [hashtable]) { throw "Objeto requerido: $key" }
                foreach ($level in $overrides[$key].Keys) {
                    if ($level -notin @('warn', 'crit')) { throw "Clave desconocida: $key.$level" }
                    $config[$key][$level] = $overrides[$key][$level]
                }
            } else { $config[$key] = $overrides[$key] }
        }
    }
    $ranges = @{ cpuRootPct = 100; commitRatio = 1; 'freeBytes.C:' = 9007199254740991; 'freeRatio.E:' = 1; eLatencyMs = 1000000000 }
    foreach ($key in $ranges.Keys) {
        foreach ($level in @('warn', 'crit')) {
            $v = $config[$key][$level]
            if (-not (Test-DesktopNumber $v) -or $v -lt 0 -or $v -gt $ranges[$key]) { throw "Umbral invalido: $key.$level" }
        }
        $low = $key.StartsWith('free')
        if (($low -and $config[$key].warn -le $config[$key].crit) -or
            (-not $low -and $config[$key].warn -ge $config[$key].crit)) { throw "Orden warn/crit invalido: $key" }
    }
    foreach ($key in @('CooldownMin', 'CooldownUnknownMin', 'MaxFileBytes', 'MaxDirBytes')) {
        $v = $config[$key]
        $min = 0; $max = 525600
        if ($key.StartsWith('Max')) { $min = 1024; $max = 9007199254740991 }
        if (-not (Test-DesktopNumber $v) -or $v -lt $min -or $v -gt $max -or
            ($key.StartsWith('Max') -and $v -ne [math]::Floor($v))) { throw "Limite invalido: $key" }
    }
    if ($config.MaxDirBytes -lt $config.MaxFileBytes) { throw 'MaxDirBytes menor que MaxFileBytes.' }
    return $config
}

function New-DesktopMetric {
    param($Value, [string]$Unit, [string]$Source, [string]$ErrorText, [string]$Status = 'ok')
    $errorValue = $null
    if ($ErrorText) { $Value = $null; $Status = 'unknown'; $errorValue = $ErrorText }
    return @{ value = $Value; unit = $Unit; status = $Status; error = $errorValue; source = $Source }
}

function Get-DesktopNumber {
    param($Object, [string]$Property, [decimal]$Min = 0, [decimal]$Max = [decimal]::MaxValue)
    $value = $Object.$Property
    if (-not (Test-DesktopNumber $value)) { throw "Propiedad ausente o no numerica: $Property" }
    if ([decimal]$value -lt $Min -or [decimal]$value -gt $Max) { throw "Propiedad fuera de rango: $Property" }
    return [decimal]$value
}

function Get-AvgTimerMs {
    param($Before, $After)
    $source = 'Win32_PerfRawData_PerfDisk_LogicalDisk[E:].AvgDisksecPerTransfer'
    try {
        if ($Before.Name -ne 'E:' -or $After.Name -ne 'E:') { throw 'Instancia raw E: ausente.' }
        $n0 = Get-DesktopNumber $Before 'AvgDisksecPerTransfer'
        $n1 = Get-DesktopNumber $After 'AvgDisksecPerTransfer'
        $b0 = Get-DesktopNumber $Before 'AvgDisksecPerTransfer_Base'
        $b1 = Get-DesktopNumber $After 'AvgDisksecPerTransfer_Base'
        $f0 = Get-DesktopNumber $Before 'Frequency_PerfTime' 1
        $f1 = Get-DesktopNumber $After 'Frequency_PerfTime' 1
        if ($f0 -ne $f1) { throw 'Frequency_PerfTime cambio.' }
        foreach ($property in @('Timestamp_PerfTime', 'Timestamp_Sys100NS')) {
            $hasBefore = $null -ne $Before.PSObject.Properties[$property]
            $hasAfter = $null -ne $After.PSObject.Properties[$property]
            if ($Before -is [Collections.IDictionary]) { $hasBefore = $Before.Contains($property) }
            if ($After -is [Collections.IDictionary]) { $hasAfter = $After.Contains($property) }
            if ($hasBefore -or $hasAfter) {
                $t0 = Get-DesktopNumber $Before $property
                $t1 = Get-DesktopNumber $After $property
                if ($t1 -le $t0) { throw "Timestamp no creciente: $property" }
            }
        }
        if ($n1 -lt $n0 -or $b1 -lt $b0) { throw 'Contador reiniciado.' }
        $delta = $b1 - $b0
        if ($delta -eq 0) { $m = New-DesktopMetric $null 'ms' $source $null 'idle' }
        else { $m = New-DesktopMetric (($n1 - $n0) / $f1 / $delta * 1000) 'ms' $source }
        $m.transfers = $delta
        return $m
    } catch { return (New-DesktopMetric $null 'ms' $source $_.Exception.Message) }
}

function Set-DesktopLevel {
    param($Metric, $Threshold, [switch]$Low)
    if ($Metric.status -ne 'ok') { return }
    if (($Low -and $Metric.value -lt $Threshold.crit) -or (-not $Low -and $Metric.value -ge $Threshold.crit)) { $Metric.status = 'crit' }
    elseif (($Low -and $Metric.value -lt $Threshold.warn) -or (-not $Low -and $Metric.value -ge $Threshold.warn)) { $Metric.status = 'warn' }
}

function Read-DesktopRaw {
    param([int]$VentanaMs)
    $queries = [ordered]@{
        Processor = @('Win32_PerfFormattedData_PerfOS_Processor', "Name='_Total'")
        Memory = @('Win32_PerfFormattedData_PerfOS_Memory', $null)
        Disks = @('Win32_LogicalDisk', "DeviceID='C:' OR DeviceID='E:'")
        HvVp = @('Win32_PerfFormattedData_HvStats_HyperVHypervisorVirtualProcessor', $null)
        Os = @('Win32_OperatingSystem', $null)
        DiskRaw0 = @('Win32_PerfRawData_PerfDisk_LogicalDisk', "Name='E:'")
        DiskRaw1 = @('Win32_PerfRawData_PerfDisk_LogicalDisk', "Name='E:'")
    }
    $raw = @{ WindowMs = $VentanaMs }
    foreach ($key in $queries.Keys) {
        if ($key -eq 'DiskRaw1') { Start-Sleep -Milliseconds $VentanaMs }
        try {
            $argsCim = @{ ClassName = $queries[$key][0]; ErrorAction = 'Stop' }
            if ($queries[$key][1]) { $argsCim.Filter = $queries[$key][1] }
            $raw[$key] = @{ obj = @(Get-CimInstance @argsCim); error = $null }
        } catch { $raw[$key] = @{ obj = @(); error = $_.Exception.Message } }
    }
    $raw.ReadUtc = [datetime]::UtcNow
    return $raw
}

function ConvertTo-DesktopSample {
    param([hashtable]$Raw, [hashtable]$Thresholds, [string]$HostName)
    $metrics = [ordered]@{}
    foreach ($spec in @(
        @('cpuRootPct', 'Processor', 'PercentProcessorTime', 'pct', 100),
        @('commitBytes', 'Memory', 'CommittedBytes', 'bytes', [decimal]::MaxValue),
        @('commitLimitBytes', 'Memory', 'CommitLimit', 'bytes', [decimal]::MaxValue))) {
        $entry = $Raw[$spec[1]]
        $class = 'Win32_PerfFormattedData_PerfOS_Memory'
        if ($spec[1] -eq 'Processor') { $class = 'Win32_PerfFormattedData_PerfOS_Processor[_Total]' }
        try {
            if ($entry.error) { throw $entry.error }
            $objects = @($entry.obj)
            if ($objects.Count -ne 1) { throw "Instancia unica requerida: $class" }
            $v = Get-DesktopNumber $objects[0] $spec[2] 0 $spec[4]
            if ($spec[0] -eq 'commitLimitBytes' -and $v -eq 0) { throw 'CommitLimit debe ser positivo.' }
            $metrics[$spec[0]] = New-DesktopMetric $v $spec[3] "$class.$($spec[2])"
        } catch { $metrics[$spec[0]] = New-DesktopMetric $null $spec[3] "$class.$($spec[2])" $_.Exception.Message }
    }
    if ($metrics.commitBytes.status -eq 'unknown' -or $metrics.commitLimitBytes.status -eq 'unknown') {
        $metrics.commitRatio = New-DesktopMetric $null 'ratio' 'CommittedBytes/CommitLimit' 'Commit o limite desconocido.'
    } else {
        $metrics.commitRatio = New-DesktopMetric ($metrics.commitBytes.value / $metrics.commitLimitBytes.value) 'ratio' 'CommittedBytes/CommitLimit'
    }
    if ($Raw.DiskRaw0.error -or $Raw.DiskRaw1.error) {
        $metrics.eLatencyMs = New-DesktopMetric $null 'ms' 'Win32_PerfRawData_PerfDisk_LogicalDisk[E:]' (($Raw.DiskRaw0.error, $Raw.DiskRaw1.error | Where-Object { $_ }) -join '; ')
    } else { $metrics.eLatencyMs = Get-AvgTimerMs @($Raw.DiskRaw0.obj)[0] @($Raw.DiskRaw1.obj)[0] }
    $metrics.eLatencyMs.windowMs = $Raw.WindowMs
    foreach ($key in @('cpuRootPct', 'commitRatio', 'eLatencyMs')) { Set-DesktopLevel $metrics[$key] $Thresholds[$key] }
    $volumes = [ordered]@{}
    foreach ($name in @('C:', 'E:')) {
        try {
            if ($Raw.Disks.error) { throw $Raw.Disks.error }
            $disk = @($Raw.Disks.obj | Where-Object { $_.DeviceID -eq $name })
            if ($disk.Count -ne 1) { throw "Win32_LogicalDisk sin instancia unica $name" }
            $total = Get-DesktopNumber $disk[0] 'Size' 1
            $free = Get-DesktopNumber $disk[0] 'FreeSpace' 0 $total
            $value = $free; $key = 'freeBytes.C:'
            if ($name -eq 'E:') { $value = $free / $total; $key = 'freeRatio.E:' }
            $m = New-DesktopMetric $value 'bytes' 'Win32_LogicalDisk'
            Set-DesktopLevel $m $Thresholds[$key] -Low
            $volumes[$name] = @{ freeBytes = $free; totalBytes = $total; status = $m.status; error = $null }
        } catch { $volumes[$name] = @{ freeBytes = $null; totalBytes = $null; status = 'unknown'; error = $_.Exception.Message } }
    }
    $guest = @{ status = 'ok'; error = $null; note = 'Tiempo de invitado por vCPU; no es CPU del host ni identifica WSL.'; series = @() }
    try {
        if ($Raw.HvVp.error) { throw $Raw.HvVp.error }
        if ($null -eq $Raw.HvVp.obj) { throw 'Respuesta Hyper-V ausente.' }
        $seen = @{}
        foreach ($vp in $Raw.HvVp.obj) {
            if ($vp.Name -isnot [string] -or [string]::IsNullOrWhiteSpace($vp.Name)) { throw 'Name Hyper-V ausente.' }
            if ($vp.Name -like '*_Total*') { continue }
            if ($seen.ContainsKey($vp.Name)) { throw 'Name Hyper-V duplicado.' }
            $seen[$vp.Name] = $true
            $guest.series += @{ name = $vp.Name; pct = (Get-DesktopNumber $vp 'PercentGuestRunTime' 0 100) }
        }
    } catch { $guest.status = 'unknown'; $guest.error = $_.Exception.Message; $guest.series = @() }
    $boot = @{ bootId = $null; lastBootUtc = $null; error = $null }
    try {
        if ($Raw.Os.error) { throw $Raw.Os.error }
        $bootDate = @($Raw.Os.obj)[0].LastBootUpTime
        if ($bootDate -isnot [datetime]) { throw 'LastBootUpTime ausente o no DateTime.' }
        $boot.lastBootUtc = $bootDate.ToUniversalTime().ToString('o')
        $boot.bootId = "$HostName/$($boot.lastBootUtc)"
    } catch { $boot.error = $_.Exception.Message }
    $errors = @()
    $levels = @()
    foreach ($key in $metrics.Keys) {
        $levels += $metrics[$key].status
        if ($metrics[$key].error) { $errors += "$key`: $($metrics[$key].error)" }
    }
    foreach ($key in $volumes.Keys) {
        $levels += $volumes[$key].status
        if ($volumes[$key].error) { $errors += "volumes.$key`: $($volumes[$key].error)" }
    }
    $levels += $guest.status
    if ($guest.error) { $errors += "guestRuntime: $($guest.error)" }
    if ($boot.error) { $errors += "boot: $($boot.error)"; $levels += 'unknown' }
    $status = 'ok'
    foreach ($level in @('unknown', 'warn', 'crit')) { if ($levels -contains $level) { $status = $level } }
    return @{ schema = 'desktop-vigilante/muestra@1'; ts = $Raw.ReadUtc.ToUniversalTime().ToString('o'); host = $HostName;
        boot = $boot; status = $status; metrics = $metrics; volumes = $volumes; guestRuntime = $guest;
        persist = @{ jsonl = 'disabled'; error = $null; alerts = 'disabled' }; errors = @($errors) }
}

function New-DesktopState {
    return @{ schema = 'desktop-vigilante/estado@1'; bootId = $null; keys = @{}; capNoticeUtc = $null }
}

function Select-DesktopAlerts {
    param($Sample, $State, [datetime]$NowUtc, [hashtable]$Thresholds)
    $NowUtc = $NowUtc.ToUniversalTime()
    $next = New-DesktopState
    $next.bootId = $Sample.boot.bootId
    $next.capNoticeUtc = $State.capNoticeUtc
    $entries = [ordered]@{}
    foreach ($key in $Sample.metrics.Keys) { $entries[$key] = $Sample.metrics[$key] }
    foreach ($name in $Sample.volumes.Keys) {
        $v = $Sample.volumes[$name]
        $entries["volume.$name"] = @{ status = $v.status; value = $v.freeBytes; error = $v.error }
    }
    $entries.guestRuntime = $Sample.guestRuntime
    $entries.boot = @{ status = 'ok'; error = $Sample.boot.error; value = $null }
    if ($Sample.boot.error) { $entries.boot.status = 'unknown' }
    $alerts = @()
    foreach ($key in $entries.Keys) {
        $entry = $entries[$key]; $level = $entry.status; $old = $State.keys[$key]
        if ($level -eq 'idle') {
            if ($old) { $next.keys[$key] = $old }
            continue
        }
        $previous = 'ok'
        if ($old) { $previous = $old.level }
        $elapsed = [double]::PositiveInfinity
        if ($old -and $old.lastAlertUtc -and $State.bootId -eq $Sample.boot.bootId) {
            $elapsed = ($NowUtc - [datetime]::Parse($old.lastAlertUtc).ToUniversalTime()).TotalMinutes
            if ($elapsed -lt 0) { $elapsed = [double]::PositiveInfinity }
        }
        $kind = $null
        if ($level -eq 'unknown') {
            if ($previous -ne 'unknown' -or $elapsed -ge $Thresholds.CooldownUnknownMin) { $kind = 'unknown' }
        } elseif ($level -in @('warn', 'crit')) {
            if ($previous -ne $level) { $kind = 'enter' }
            elseif ($elapsed -ge $Thresholds.CooldownMin) { $kind = 'repeat' }
        } elseif ($previous -ne 'ok') { $kind = 'recover' }
        $last = $null
        if ($old) { $last = $old.lastAlertUtc }
        if ($kind) {
            $last = $NowUtc.ToUniversalTime().ToString('o')
            $message = "$key $previous -> $level"
            if ($entry.error) { $message += " ($($entry.error))" }
            $alerts += @{ schema = 'desktop-vigilante/aviso@1'; ts = $last; host = $Sample.host; key = $key;
                level = $level; previous = $previous; value = $entry.value; kind = $kind; message = $message }
        }
        $next.keys[$key] = @{ level = $level; lastAlertUtc = $last }
    }
    return @{ alerts = @($alerts); state = $next }
}

function Get-DesktopMutexName {
    param([string]$StateDir)
    $path = [IO.Path]::GetFullPath($StateDir).TrimEnd('\', '/').ToUpperInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($path))).Replace('-', '') }
    finally { $sha.Dispose() }
    return "Global\desktop-vigilante-$hash"
}

function Read-DesktopState {
    param([string]$StateDir)
    $path = Join-Path $StateDir 'estado-windows.json'
    if (-not [IO.File]::Exists($path)) { return (New-DesktopState) }
    try {
        $state = ConvertTo-DesktopMap ([IO.File]::ReadAllText($path) | ConvertFrom-Json -ErrorAction Stop)
        if ($state -isnot [hashtable] -or $state.schema -ne 'desktop-vigilante/estado@1' -or
            $state.keys -isnot [hashtable] -or -not $state.ContainsKey('bootId') -or
            -not $state.ContainsKey('capNoticeUtc')) { throw 'Esquema de estado invalido.' }
        $allowed = @('cpuRootPct', 'commitBytes', 'commitLimitBytes', 'commitRatio', 'eLatencyMs', 'volume.C:', 'volume.E:', 'guestRuntime', 'boot')
        foreach ($key in $state.keys.Keys) {
            $entry = $state.keys[$key]
            if ($key -notin $allowed -or $entry -isnot [hashtable] -or
                $entry.level -notin @('ok', 'warn', 'crit', 'unknown')) { throw 'Entrada de estado invalida.' }
            if ($entry.lastAlertUtc) { [void][datetime]::Parse($entry.lastAlertUtc) }
        }
        if ($state.capNoticeUtc) { [void][datetime]::Parse($state.capNoticeUtc) }
        return $state
    } catch {
        $reason = $_.Exception.Message
        $backup = Join-Path $StateDir 'estado-windows.corrupto.json'
        if ([IO.File]::Exists($backup)) { throw "Estado corrupto; copia previa conservada. Resolver $path antes de escribir. $reason" }
        [IO.File]::Copy($path, $backup, $false)
        $empty = New-DesktopState
        $empty.readError = "Estado corrupto conservado en $backup. $reason"
        return $empty
    }
}

function Write-DesktopAtomic {
    param([string]$Path, $Value)
    $temp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temp, (ConvertTo-Json -InputObject $Value -Depth 12 -Compress), (New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp, $Path, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temp, $Path) }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Add-DesktopJsonLine {
    param([string]$StateDir, [string]$Name, $Value, [hashtable]$Limits)
    $line = (ConvertTo-Json -InputObject $Value -Depth 12 -Compress) + "`n"
    $encoding = New-Object Text.UTF8Encoding($false)
    $bytes = $encoding.GetByteCount($line)
    $path = Join-Path $StateDir $Name
    $length = 0
    if ([IO.File]::Exists($path)) { $length = (Get-Item -LiteralPath $path -ErrorAction Stop).Length }
    $total = 0L
    foreach ($file in Get-ChildItem -LiteralPath $StateDir -File -Force -ErrorAction Stop) { $total += $file.Length }
    if ($length + $bytes -gt $Limits.MaxFileBytes -or $total + $bytes -gt $Limits.MaxDirBytes) { return 'capped' }
    [IO.File]::AppendAllText($path, $line, $encoding)
    return 'written'
}

function Save-DesktopRecord {
    param([string]$StateDir, $Sample, $Alerts, $NewState, [hashtable]$Limits)
    $date = ([datetime]::Parse($Sample.ts)).ToUniversalTime().ToString('yyyy-MM-dd')
    $Sample.persist.jsonl = 'written'; $Sample.persist.alerts = 'written'
    try {
        foreach ($alert in $Alerts) {
            [Console]::Error.WriteLine($alert.message)
            if ((Add-DesktopJsonLine $StateDir "avisos-$date.jsonl" $alert $Limits) -eq 'capped') { $Sample.persist.alerts = 'capped' }
        }
        # Control files remain fresh at the cap; their fixed-size overhead is outside the append budget.
        Write-DesktopAtomic (Join-Path $StateDir 'estado-windows.json') $NewState
        Write-DesktopAtomic (Join-Path $StateDir 'ultimo.json') $Sample
        $Sample.persist.jsonl = Add-DesktopJsonLine $StateDir "muestras-$date.jsonl" $Sample $Limits
        if ($Sample.persist.jsonl -eq 'capped' -or $Sample.persist.alerts -eq 'capped') {
            $now = [datetime]::Parse($Sample.ts).ToUniversalTime()
            $last = $null
            if ($NewState.capNoticeUtc) { $last = [datetime]::Parse($NewState.capNoticeUtc).ToUniversalTime() }
            if ($null -eq $last -or $last.Date -ne $now.Date -or $last -gt $now) {
                $notice = @{ schema = 'desktop-vigilante/aviso@1'; ts = $Sample.ts; host = $Sample.host;
                    key = 'persist'; level = 'warn'; kind = 'capped'; message = 'persist capped: historial lleno; ultimo.json sigue fresco.' }
                [Console]::Error.WriteLine($notice.message)
                if ((Add-DesktopJsonLine $StateDir "avisos-$date.jsonl" $notice $Limits) -eq 'capped') { $Sample.persist.alerts = 'capped' }
                $NewState.capNoticeUtc = $Sample.ts
                Write-DesktopAtomic (Join-Path $StateDir 'estado-windows.json') $NewState
            }
        }
        Write-DesktopAtomic (Join-Path $StateDir 'ultimo.json') $Sample
    } catch {
        $Sample.persist.error = $_.Exception.Message
        $Sample.persist.jsonl = 'failed'
        $Sample.errors += "persist: $($Sample.persist.error)"
        [Console]::Error.WriteLine("persist failed: $($Sample.persist.error)")
        try { Write-DesktopAtomic (Join-Path $StateDir 'ultimo.json') $Sample } catch { [Console]::Error.WriteLine($_.Exception.Message) }
    }
}

function Invoke-DesktopVigilante {
    param([string]$StateDir, [string]$ConfigPath, [switch]$NoPersist, $VentanaMs = 1000)
    try {
        $window = 0
        if (-not [int]::TryParse([string]$VentanaMs, [ref]$window) -or $window -lt 250 -or $window -gt 5000) { throw 'VentanaMs debe ser entero entre 250 y 5000.' }
        $config = Read-DesktopConfig $ConfigPath
        if (-not $NoPersist) {
            if (-not $StateDir) { $StateDir = Join-Path $env:LOCALAPPDATA 'pao\desktop-vigilante' }
            $StateDir = [IO.Path]::GetFullPath($StateDir)
        }
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        [Console]::Out.WriteLine((ConvertTo-Json -Compress @{ schema = 'desktop-vigilante/error@1'; error = $_.Exception.Message }))
        return 2
    }
    $sample = ConvertTo-DesktopSample (Read-DesktopRaw $window) $config $env:COMPUTERNAME
    $mutex = $null; $held = $false
    try {
        if ($NoPersist) {
            $selected = Select-DesktopAlerts $sample (New-DesktopState) ([datetime]::Parse($sample.ts)) $config
            foreach ($alert in $selected.alerts) { [Console]::Error.WriteLine($alert.message) }
        } else {
            $mutex = New-Object Threading.Mutex($false, (Get-DesktopMutexName $StateDir))
            try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
            if (-not $held) { $sample.persist.jsonl = 'busy'; $sample.persist.alerts = 'busy' }
            else {
                [void][IO.Directory]::CreateDirectory($StateDir)
                $state = Read-DesktopState $StateDir
                if ($state.readError) {
                    $sample.persist.error = $state.readError
                    $sample.errors += "persist: $($state.readError)"
                    [Console]::Error.WriteLine($state.readError)
                }
                $selected = Select-DesktopAlerts $sample $state ([datetime]::Parse($sample.ts)) $config
                Save-DesktopRecord $StateDir $sample $selected.alerts $selected.state $config
            }
        }
    } catch {
        $sample.persist.jsonl = 'failed'; $sample.persist.error = $_.Exception.Message
        $sample.errors += "persist: $($sample.persist.error)"
        [Console]::Error.WriteLine("persist failed: $($sample.persist.error)")
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $sample -Depth 12 -Compress))
    if ($sample.persist.error) { return 3 }
    return 0
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($args.Count) {
        [Console]::Error.WriteLine('Parametros desconocidos.')
        [Console]::Out.WriteLine('{"schema":"desktop-vigilante/error@1","error":"Parametros desconocidos."}')
        exit 2
    }
    exit (Invoke-DesktopVigilante @PSBoundParameters)
}
