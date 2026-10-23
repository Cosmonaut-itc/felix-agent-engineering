param([string]$TestRoot = (Join-Path $env:TEMP ('pao-desktop-watch-tests-' + [guid]::NewGuid().ToString('N'))))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vigilante.ps1')
[void][IO.Directory]::CreateDirectory($TestRoot)
$script:passed = 0; $script:failed = 0

function Assert { param($Condition, [string]$Message) if (-not $Condition) { throw $Message } }
function Test {
    param([string]$Name, [scriptblock]$Body)
    try { & $Body; $script:passed++; Write-Output "PASS $Name" }
    catch { $script:failed++; Write-Output "FAIL $Name -- $($_.Exception.Message)" }
}
function New-Raw {
    return @{
        Processor = @{ obj = @(@{ PercentProcessorTime = 37 }); error = $null }
        Memory = @{ obj = @(@{ CommittedBytes = 80; CommitLimit = 100 }); error = $null }
        Disks = @{ obj = @(@{ DeviceID = 'C:'; FreeSpace = 50GB; Size = 100GB }, @{ DeviceID = 'E:'; FreeSpace = 95GB; Size = 100GB }); error = $null }
        DiskRaw0 = @{ obj = @(@{ Name = 'E:'; AvgDisksecPerTransfer = [uint64]::Parse('18446744073709000001'); AvgDisksecPerTransfer_Base = 10; Frequency_PerfTime = 10000000; Timestamp_PerfTime = 100 }); error = $null }
        DiskRaw1 = @{ obj = @(@{ Name = 'E:'; AvgDisksecPerTransfer = [uint64]::Parse('18446744073709240001'); AvgDisksecPerTransfer_Base = 12; Frequency_PerfTime = 10000000; Timestamp_PerfTime = 200 }); error = $null }
        HvVp = @{ obj = @(@{ Name = 'GUID:Hv VP 0'; PercentGuestRunTime = 12 }, @{ Name = '_Total'; PercentGuestRunTime = 100 }); error = $null }
        Os = @{ obj = @(@{ LastBootUpTime = [datetime]'2026-10-20T08:00:00Z' }); error = $null }
        ReadUtc = [datetime]'2026-10-23T17:00:00Z'; WindowMs = 1000
    }
}
function New-Sample { param($Raw = (New-Raw)) return (ConvertTo-DesktopSample $Raw (Read-DesktopConfig) 'TEST-HOST') }
function Invoke-Captured {
    param([hashtable]$Arguments)
    $stdout = New-Object IO.StringWriter; $stderr = New-Object IO.StringWriter
    $oldOut = [Console]::Out; $oldErr = [Console]::Error
    try {
        [Console]::SetOut($stdout); [Console]::SetError($stderr)
        $code = @(Invoke-DesktopVigilante @Arguments)
        return @{ code = $code; stdout = $stdout.ToString(); stderr = $stderr.ToString() }
    } finally { [Console]::SetOut($oldOut); [Console]::SetError($oldErr); $stdout.Dispose(); $stderr.Dispose() }
}

Test 'PS5.1 parser accepts production script' {
    $tokens = $null; $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'vigilante.ps1'), [ref]$tokens, [ref]$parseErrors)
    Assert (@($parseErrors).Count -eq 0) ($parseErrors -join '; ')
}
Test 'raw average timer preserves 12ms above double integer precision' {
    $r = New-Raw; $m = Get-AvgTimerMs $r.DiskRaw0.obj[0] $r.DiskRaw1.obj[0]
    Assert ($m.status -eq 'ok' -and $m.value -eq 12 -and $m.transfers -eq 2) 'Expected 12ms from two transfers.'
}
Test 'idle is null, never zero latency' {
    $r = New-Raw; $r.DiskRaw1.obj[0].AvgDisksecPerTransfer_Base = 10
    $m = Get-AvgTimerMs $r.DiskRaw0.obj[0] $r.DiskRaw1.obj[0]
    Assert ($m.status -eq 'idle' -and $null -eq $m.value -and $null -eq $m.error) 'Idle invariant.'
}
Test 'raw timestamp rollback is unknown even with increasing counters' {
    $r = New-Raw; $r.DiskRaw1.obj[0].Timestamp_PerfTime = 99
    $m = Get-AvgTimerMs $r.DiskRaw0.obj[0] $r.DiskRaw1.obj[0]
    Assert ($m.status -eq 'unknown' -and $null -eq $m.value -and $m.error) 'Rollback must invalidate 12ms.'
}
Test 'raw reset, nulls and changed frequency are unknown' {
    foreach ($case in @('numerator', 'base', 'frequency', 'null', 'missing', 'bool', 'timestamp')) {
        $r = New-Raw; $a = $r.DiskRaw1.obj[0]
        switch ($case) {
            numerator { $a.AvgDisksecPerTransfer = 0 }
            base { $a.AvgDisksecPerTransfer_Base = 9 }
            frequency { $a.Frequency_PerfTime = 2 }
            null { $a.Frequency_PerfTime = $null }
            missing { $a.Remove('AvgDisksecPerTransfer') }
            bool { $a.AvgDisksecPerTransfer = $false }
            timestamp { $a.Timestamp_PerfTime = $null }
        }
        $m = Get-AvgTimerMs $r.DiskRaw0.obj[0] $a
        Assert ($m.status -eq 'unknown' -and $null -eq $m.value -and $m.error) "Unknown expected for $case."
    }
}
Test 'sample uses actual commit limit, root CPU and unaggregated guest names' {
    $s = New-Sample
    Assert ($s.metrics.commitRatio.value -eq 0.8 -and $s.metrics.cpuRootPct.value -eq 37) 'Host metrics.'
    Assert ($s.guestRuntime.series.Count -eq 1 -and $s.guestRuntime.series[0].name -eq 'GUID:Hv VP 0') 'Guest series excludes total.'
    Assert ($s.volumes['E:'].freeBytes -eq 95GB -and $s.volumes['E:'].status -eq 'ok') '95 percent free is valid.'
    Assert ($s.metrics.eLatencyMs.windowMs -eq 1000 -and $s.status -eq 'ok') 'Sample status/window.'
}
Test 'missing CIM properties remain unknown and crit wins globally' {
    $r = New-Raw; $r.Processor.obj[0].PercentProcessorTime = $null
    $r.Memory.obj[0].CommittedBytes = 99; $r.Memory.obj[0].CommitLimit = 100
    $r.Disks.obj[1].FreeSpace = $null; $r.HvVp.error = 'Invalid class'
    $s = New-Sample $r
    Assert ($s.metrics.cpuRootPct.status -eq 'unknown' -and $null -eq $s.metrics.cpuRootPct.value) 'CPU null.'
    Assert ($s.volumes['E:'].status -eq 'unknown' -and $null -eq $s.volumes['E:'].freeBytes) 'FreeSpace null.'
    Assert ($s.guestRuntime.status -eq 'unknown' -and $s.status -eq 'crit') 'Unknown must not hide crit.'
    Assert ($s.errors -is [array] -and $s.errors.Count -ge 3) 'Errors array.'
}
Test 'unknown dominates ok; empty valid guests differ from missing response' {
    $r = New-Raw; $r.HvVp.obj = @(); $s = New-Sample $r
    Assert ($s.guestRuntime.status -eq 'ok' -and $s.guestRuntime.series.Count -eq 0) 'Valid empty guest query.'
    $r.HvVp.obj = $null; $s = New-Sample $r
    Assert ($s.guestRuntime.status -eq 'unknown' -and $s.status -eq 'unknown') 'Missing guest response.'
    $r = New-Raw; $r.Memory.obj[0].CommitLimit = 0; $s = New-Sample $r
    Assert ($s.metrics.commitRatio.status -eq 'unknown' -and $null -eq $s.metrics.commitRatio.value) 'No zero divisor.'
}
Test 'config permits partial overrides and rejects JSON coercions and unknown keys' {
    $path = Join-Path $TestRoot 'config.json'
    [IO.File]::WriteAllText($path, '{"cpuRootPct":{"warn":92}}')
    $c = Read-DesktopConfig $path
    Assert ($c.cpuRootPct.warn -eq 92 -and $c.cpuRootPct.crit -eq 98 -and $c.commitRatio.warn -eq 0.85) 'Canonical defaults merge.'
    foreach ($json in @('{"cpuRootPct":{"warn":null}}', '{"cpuRootPct":{"warn":"92"}}', '{"cpuRootPct":{"warn":true}}', '{"cpuRootPct":{"warn":99}}', '{"commitRatio":{"crit":1.1}}', '{"cpuRootPct":{"oops":1}}', '{"unknown":1}', '{"MaxFileBytes":1.5}', '{"MaxDirBytes":1024}', '{"CooldownMin":null}', '{"cpuRootPct":{"warn":[92]}}', '{"CooldownMin":[30]}', '[{"CooldownMin":30}]', 'null', '[]')) {
        [IO.File]::WriteAllText($path, $json); $rejected = $false
        try { [void](Read-DesktopConfig $path) } catch { $rejected = $true }
        Assert $rejected "Accepted invalid JSON config $json"
    }
    Assert (-not (Test-DesktopNumber ([double]::NaN)) -and -not (Test-DesktopNumber ([double]::PositiveInfinity))) 'Finite numbers only.'
}
Test 'alerts enter, repeat, escalate and recover once' {
    $config = Read-DesktopConfig; $s = New-Sample; $s.metrics.commitRatio.status = 'warn'; $now = [datetime]$s.ts
    $a = Select-DesktopAlerts $s (New-DesktopState) $now $config
    Assert ($a.alerts.Count -eq 1 -and $a.alerts[0].kind -eq 'enter') 'Enter warn.'
    $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(1) $config
    Assert ($b.alerts.Count -eq 0) 'Cooldown suppresses repeat.'
    $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(30) $config
    Assert ($b.alerts.Count -eq 1 -and $b.alerts[0].kind -eq 'repeat') 'Repeat at boundary.'
    $s.metrics.commitRatio.status = 'crit'; $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(1) $config
    Assert ($b.alerts[0].kind -eq 'enter' -and $b.alerts[0].previous -eq 'warn') 'Escalation bypasses cooldown.'
    $s.metrics.commitRatio.status = 'ok'; $a = Select-DesktopAlerts $s $b.state $now.AddMinutes(2) $config
    Assert ($a.alerts.Count -eq 1 -and $a.alerts[0].kind -eq 'recover') 'Recover once.'
    $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(3) $config
    Assert ($b.alerts.Count -eq 0) 'No second recover.'
}
Test 'unknown cooldown survives idle; reboot and clock rollback allow warnings' {
    $c = Read-DesktopConfig; $s = New-Sample; $now = [datetime]$s.ts
    $s.metrics.eLatencyMs.status = 'unknown'; $s.metrics.eLatencyMs.error = 'Unavailable'; $s.metrics.eLatencyMs.value = $null
    $a = Select-DesktopAlerts $s (New-DesktopState) $now $c
    Assert ($a.alerts.Count -eq 1 -and $a.alerts[0].kind -eq 'unknown') 'Unknown first alert.'
    $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(179) $c
    Assert ($b.alerts.Count -eq 0) 'Own unknown cooldown.'
    $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(180) $c
    Assert ($b.alerts.Count -eq 1) 'Unknown repeats.'
    $s.metrics.eLatencyMs.status = 'idle'; $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(1) $c
    Assert ($b.alerts.Count -eq 0 -and $b.state.keys.eLatencyMs.level -eq 'unknown') 'Idle does not recover unknown.'
    $s.metrics.eLatencyMs.status = 'unknown'; $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(-1) $c
    Assert ($b.alerts.Count -eq 1) 'Clock rollback.'
    $s.boot.bootId = 'new-boot'; $b = Select-DesktopAlerts $s $a.state $now.AddMinutes(1) $c
    Assert ($b.alerts.Count -eq 1) 'Reboot.'
}
Test 'persistence replaces snapshots and appends one valid line per sample' {
    $dir = Join-Path $TestRoot 'persist'; [void][IO.Directory]::CreateDirectory($dir)
    $c = Read-DesktopConfig; $s = New-Sample; $state = (Select-DesktopAlerts $s (New-DesktopState) ([datetime]$s.ts) $c).state
    Save-DesktopRecord $dir $s @() $state $c
    $s.ts = ([datetime]$s.ts).AddMinutes(1).ToString('o'); Save-DesktopRecord $dir $s @() $state $c
    $snapshot = Get-Content -LiteralPath (Join-Path $dir 'ultimo.json') -Raw | ConvertFrom-Json
    Assert ($snapshot.ts -eq $s.ts -and $snapshot.persist.jsonl -eq 'written') 'Fresh snapshot.'
    $lines = @(Get-Content -LiteralPath (Join-Path $dir 'muestras-2026-10-23.jsonl'))
    Assert ($lines.Count -eq 2 -and ($lines[1] | ConvertFrom-Json).ts -eq $s.ts) 'Two complete records.'
    Assert (@(Get-ChildItem -LiteralPath $dir -Filter '*.tmp').Count -eq 0) 'Atomic temp files removed.'
    Assert ((Read-DesktopState $dir).schema -eq 'desktop-vigilante/estado@1') 'Readable cooldown.'
}
Test 'file and directory caps keep snapshot current and publish dropped alerts' {
    foreach ($cap in @('file', 'directory')) {
        $dir = Join-Path $TestRoot $cap; [void][IO.Directory]::CreateDirectory($dir)
        $c = Read-DesktopConfig; $c.MaxFileBytes = 1024
        if ($cap -eq 'directory') { $c.MaxFileBytes = 1MB; $c.MaxDirBytes = 1024 }
        $s = New-Sample; $s.metrics.commitRatio.status = 'warn'
        $selected = Select-DesktopAlerts $s (New-DesktopState) ([datetime]$s.ts) $c
        $log = Join-Path $dir 'avisos-2026-10-23.jsonl'; [IO.File]::WriteAllText($log, ('x' * 1024))
        $err = New-Object IO.StringWriter; $old = [Console]::Error
        try { [Console]::SetError($err); Save-DesktopRecord $dir $s $selected.alerts $selected.state $c }
        finally { [Console]::SetError($old) }
        $snapshot = Get-Content -LiteralPath (Join-Path $dir 'ultimo.json') -Raw | ConvertFrom-Json
        Assert ($s.persist.jsonl -eq 'capped' -and $s.persist.alerts -eq 'capped' -and -not $s.persist.error) 'Cap status.'
        Assert ($snapshot.persist.jsonl -eq 'capped' -and $snapshot.persist.alerts -eq 'capped') 'Snapshot matches stdout.'
        Assert ($err.ToString().Contains('commitRatio') -and $err.ToString().Contains('persist capped')) 'Dropped alerts visible on stderr.'
        Assert ((Get-Item -LiteralPath $log).Length -eq 1024) 'Existing data unchanged.'
        $err.Dispose()
    }
}

function Read-DesktopRaw { param([int]$VentanaMs) return (New-Raw) }

Test 'exclusive alert log lock preserves both stderr alerts and reports exit 3 with matching snapshot' {
    $dir = Join-Path $TestRoot 'locked-alerts'; [void][IO.Directory]::CreateDirectory($dir)
    $raw = New-Raw; $raw.Processor.obj[0].PercentProcessorTime = 99; $raw.Memory.obj[0].CommittedBytes = 99
    $fixture = Join-Path $TestRoot 'locked-alerts-raw.json'
    [IO.File]::WriteAllText($fixture, (ConvertTo-Json -InputObject $raw -Depth 12 -Compress))
    $stdout = Join-Path $TestRoot 'locked-alerts.stdout'; $stderr = Join-Path $TestRoot 'locked-alerts.stderr'
    $code = @'
$ErrorActionPreference = 'Stop'
. 'SCRIPT_PATH'
$raw = ConvertTo-DesktopMap ([IO.File]::ReadAllText('FIXTURE_PATH') | ConvertFrom-Json)
function Read-DesktopRaw { param([int]$VentanaMs) return $raw }
exit (Invoke-DesktopVigilante -StateDir 'STATE_PATH')
'@
    $code = $code.Replace('SCRIPT_PATH', (Join-Path $PSScriptRoot 'vigilante.ps1').Replace("'", "''")).Replace('FIXTURE_PATH', $fixture.Replace("'", "''")).Replace('STATE_PATH', $dir.Replace("'", "''"))
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    $log = Join-Path $dir 'avisos-2026-10-23.jsonl'
    $lock = [IO.File]::Open($log, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $process = $null
    try {
        $process = Start-Process powershell.exe -WindowStyle Hidden -Wait -PassThru -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $json = [IO.File]::ReadAllText($stdout).Trim(); $sample = $json | ConvertFrom-Json
        $messages = [IO.File]::ReadAllText($stderr)
        $snapshot = [IO.File]::ReadAllText((Join-Path $dir 'ultimo.json'))
        $cpuPublished = $messages.Contains('cpuRootPct'); $commitPublished = $messages.Contains('commitRatio')
        Write-Output "Locked alerts exit=$($process.ExitCode) cpuStderr=$cpuPublished commitStderr=$commitPublished alerts=$($sample.persist.alerts) snapshotMatches=$($snapshot -eq $json)"
        Assert ($cpuPublished -and $commitPublished) 'Both CPU and commit alerts must reach stderr despite the exclusive log lock.'
        Assert ($sample.persist.alerts -eq 'failed') 'Locked alert log must report alerts failed.'
        Assert ($process.ExitCode -eq 3 -and $sample.persist.error -and $sample.persist.jsonl -eq 'failed') 'Append failure must produce exit 3 and persistence error JSON.'
        Assert ($snapshot -eq $json) 'Snapshot must match stdout including failed persistence.'
        Assert ($lock.Length -eq 0) 'Exclusively locked log is unchanged.'
    } finally {
        if ($null -ne $process) { $process.Dispose() }
        $lock.Dispose()
    }
}

Test 'one-shot emits exactly one JSON line and only an exit code on pipeline' {
    $dir = Join-Path $TestRoot 'no-persist'
    $r = Invoke-Captured @{ NoPersist = $true; StateDir = $dir }
    Assert ($r.code.Count -eq 1 -and $r.code[0] -eq 0) 'Only exit code returned.'
    $s = $r.stdout | ConvertFrom-Json
    Assert ($s.schema -eq 'desktop-vigilante/muestra@1' -and $s.persist.jsonl -eq 'disabled') 'JSON stdout.'
    Assert (-not [IO.Directory]::Exists($dir)) 'NoPersist creates no state directory.'
    $r = Invoke-Captured @{ NoPersist = $true; VentanaMs = 249 }
    Assert ($r.code[0] -eq 2 -and ($r.stdout | ConvertFrom-Json).error) 'Invalid usage exits 2 with JSON.'
}
Test 'misspelled NoPersist is rejected before observation or persistence' {
    $stdout = Join-Path $TestRoot 'invalid-cli.json'; $stderr = Join-Path $TestRoot 'invalid-cli.stderr'
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -NoPersit' -f (Join-Path $PSScriptRoot 'vigilante.ps1')
    $process = Start-Process powershell.exe -WindowStyle Hidden -Wait -PassThru -ArgumentList $arguments -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    try {
        $errorJson = Get-Content -LiteralPath $stdout -Raw | ConvertFrom-Json
        Assert ($process.ExitCode -eq 2 -and $errorJson.schema -eq 'desktop-vigilante/error@1') 'Unknown flag exits 2.'
        Assert ([IO.File]::ReadAllText($stderr).Length -gt 0) 'Invalid usage has stderr.'
    } finally { $process.Dispose() }
}
Test 'corrupt state is preserved once and failure is visible as exit 3 JSON' {
    $dir = Join-Path $TestRoot 'corrupt'; [void][IO.Directory]::CreateDirectory($dir)
    $path = Join-Path $dir 'estado-windows.json'; [IO.File]::WriteAllText($path, '{broken')
    $r = Invoke-Captured @{ StateDir = $dir }
    Assert ($r.code[0] -eq 3 -and ($r.stdout | ConvertFrom-Json).persist.error) 'Corruption exits 3 with JSON.'
    Assert ([IO.File]::ReadAllText((Join-Path $dir 'estado-windows.corrupto.json')) -eq '{broken') 'Original bytes preserved.'
    Assert (@(Get-ChildItem -LiteralPath $dir -Filter '*corrupto*').Count -eq 1) 'One backup.'
    $r = Invoke-Captured @{ StateDir = $dir }
    Assert ($r.code[0] -eq 0 -and -not ($r.stdout | ConvertFrom-Json).persist.error) 'Next invocation uses repaired state.'
    [IO.File]::WriteAllText($path, '{second-corruption')
    $r = Invoke-Captured @{ StateDir = $dir }
    Assert ($r.code[0] -eq 3 -and [IO.File]::ReadAllText($path) -eq '{second-corruption') 'Second corruption is preserved for operator.'
    Assert ([IO.File]::ReadAllText((Join-Path $dir 'estado-windows.corrupto.json')) -eq '{broken') 'Previous backup is not replaced.'
}
Test 'persistence IO failure remains visible in JSON and exit code' {
    $path = Join-Path $TestRoot 'not-directory'; [IO.File]::WriteAllText($path, 'owned')
    $r = Invoke-Captured @{ StateDir = $path }
    Assert ($r.code[0] -eq 3 -and ($r.stdout | ConvertFrom-Json).persist.jsonl -eq 'failed') 'IO failure contract.'
    Assert ([IO.File]::ReadAllText($path) -eq 'owned') 'No foreign deletion.'
}
Test 'mutex canonicalizes a directory and isolates other directories' {
    $a = Get-DesktopMutexName (Join-Path $TestRoot 'Lock')
    Assert ($a -eq (Get-DesktopMutexName (Join-Path $TestRoot 'lock\'))) 'Windows case and trailing separator.'
    Assert ($a -ne (Get-DesktopMutexName (Join-Path $TestRoot 'other'))) 'Separate StateDir has separate lock.'
}
Test 'another process blocks only its StateDir and cannot block NoPersist' {
    $dir = Join-Path $TestRoot 'held'; $name = Get-DesktopMutexName $dir
    $ready = Join-Path $TestRoot 'lock-ready'; $release = Join-Path $TestRoot 'lock-release'
    $code = @'
$mutex = New-Object Threading.Mutex($false, 'MUTEX_NAME')
$held = $false
try {
    $held = $mutex.WaitOne(0)
    if (-not $held) { exit 4 }
    [IO.File]::WriteAllText('READY_PATH', 'ready')
    $deadline = [datetime]::UtcNow.AddSeconds(30)
    while (-not [IO.File]::Exists('RELEASE_PATH') -and [datetime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }
} finally { if ($held) { $mutex.ReleaseMutex() }; $mutex.Dispose() }
'@
    $code = $code.Replace('MUTEX_NAME', $name).Replace('READY_PATH', $ready.Replace("'", "''")).Replace('RELEASE_PATH', $release.Replace("'", "''"))
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    $holder = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded)
    try {
        $deadline = [datetime]::UtcNow.AddSeconds(10)
        while (-not [IO.File]::Exists($ready) -and [datetime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }
        Assert ([IO.File]::Exists($ready)) 'Own lock holder started.'
        $r = Invoke-Captured @{ StateDir = $dir }
        Assert ($r.code[0] -eq 0 -and ($r.stdout | ConvertFrom-Json).persist.jsonl -eq 'busy') 'Busy emits sample.'
        Assert (-not [IO.Directory]::Exists($dir)) 'Busy writes nothing.'
        $r = Invoke-Captured @{ StateDir = $dir; NoPersist = $true }
        Assert ($r.code[0] -eq 0 -and ($r.stdout | ConvertFrom-Json).persist.jsonl -eq 'disabled') 'NoPersist ignores held mutex.'
        $r = Invoke-Captured @{ StateDir = (Join-Path $TestRoot 'independent') }
        Assert ($r.code[0] -eq 0 -and ($r.stdout | ConvertFrom-Json).persist.jsonl -eq 'written') 'Another StateDir progresses.'
    } finally {
        [IO.File]::WriteAllText($release, 'release')
        Assert ($holder.WaitForExit(15000)) 'Own lock holder exited.'
        $holder.Dispose()
    }
    $r = Invoke-Captured @{ StateDir = $dir }
    Assert ($r.code[0] -eq 0 -and ($r.stdout | ConvertFrom-Json).persist.jsonl -eq 'written') 'Mutex is released.'
}

Write-Output "$script:passed passed; $script:failed failed; artifacts $TestRoot"
if ($script:failed) { exit 1 }
exit 0
