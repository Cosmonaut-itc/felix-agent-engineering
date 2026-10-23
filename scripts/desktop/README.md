# Vigilante Windows

Monitor one-shot para PowerShell 5.1 nativo, sin administrador ni dependencias.
Sólo observa CIM. No instala tareas, cambia admisión, lanza o termina procesos,
limpia datos ni modifica Update, pagefile, AVD o `.wslconfig`. No consulta WSL.
Esta entrega queda limitada al monitor.

## Uso

Desde PowerShell nativo, en la raíz del checkout.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\desktop\vigilante.ps1 -NoPersist
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File scripts\desktop\vigilante.ps1 -StateDir "$env:LOCALAPPDATA\pao\desktop-vigilante"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\desktop\vigilante.ps1 `
    -StateDir "$env:LOCALAPPDATA\pao\desktop-vigilante" -ConfigPath C:\mi-config\umbrales.json -VentanaMs 1000
```

`-VentanaMs` acepta enteros entre 250 y 5000, por defecto 1000. Es la espera
entre las dos consultas raw de E:. Las consultas CIM añaden tiempo a la ejecución.
`-StateDir` por defecto es `%LOCALAPPDATA%\pao\desktop-vigilante`.
Es un directorio exclusivo de este monitor. Su contenido no se comparte con el
vigilante Unix. `-NoPersist` no lee ese directorio, crea copias ni toma mutex.
Al dot-sourcear el script sólo se definen funciones.

Stdout contiene una línea JSON. Stderr publica avisos humanos y errores de
persistencia. El código de salida es 0 con una muestra, incluso `crit`,
`unknown`, `capped` o `busy`; 2 con parámetros o configuración inválidos; 3
con un fallo de persistencia. El consumidor debe leer `status` y `persist`.

## Configuración

[`umbrales.json`](umbrales.json) es la única fuente de defaults. `-ConfigPath`
superpone un objeto JSON parcial, también dentro de cada par `warn` y `crit`.
Por ejemplo, `{"cpuRootPct":{"warn":92},"CooldownMin":60}` conserva los
demás defaults. Claves desconocidas, nulos, booleanos, strings numéricos,
valores no finitos, rangos inválidos y órdenes inconsistentes salen con 2.

`cpuRootPct`, `commitRatio` y `eLatencyMs` usan límites inclusivos hacia
arriba. `freeBytes.C:` y `freeRatio.E:` usan límites estrictos hacia abajo.
Los porcentajes aceptan 0 a 100 y las razones 0 a 1; la latencia, 0 a
1 000 000 000 ms; los bytes, 0 a 9 007 199 254 740 991.
`CooldownMin` y `CooldownUnknownMin` aceptan 0 a 525600 minutos.
`MaxFileBytes` y `MaxDirBytes` son enteros de 1024 a 9 007 199 254 740 991,
y el límite del directorio debe ser mayor o igual al del archivo.
Los umbrales son propuestas configurables. No controlan admisión ni procesos.

## Datos y fuentes

La muestra usa `schema=desktop-vigilante/muestra@1`, `ts` UTC, `host`,
`boot`, `status`, `metrics`, `volumes`, `guestRuntime`, `persist` y `errors`.
`errors` siempre es un array. Cada métrica contiene `value`, `unit`, `status`,
`error` y `source`. `unknown` tiene valor null y causa; `idle` tiene valor y
error null. La precedencia global es `crit > warn > unknown > ok`; idle cuenta
como ok. Un fallo de persistencia se informa por separado.

- `cpuRootPct` sale de `Win32_PerfFormattedData_PerfOS_Processor`, instancia
  `_Total`, `PercentProcessorTime`. Con Hyper-V mide la partición raíz.
- `commitBytes` y `commitLimitBytes` salen de
  `Win32_PerfFormattedData_PerfOS_Memory.CommittedBytes` y `CommitLimit`.
  `commitRatio` divide ambos, con el límite real del host.
- `volumes.C:` y `volumes.E:` contienen `freeBytes`, `totalBytes`, `status`
  y `error`, de `Win32_LogicalDisk.FreeSpace` y `Size`. Los bytes dan precisión
  y permiten umbrales absolutos. E: con 95 % libre es válido; no se observó
  un defecto de `PercentFreeSpace`.
- `eLatencyMs` usa dos lecturas de
  `Win32_PerfRawData_PerfDisk_LogicalDisk`, instancia E:.
  La fórmula es `((N1-N0)/Frequency_PerfTime)/(B1-B0)*1000` para
  `AvgDisksecPerTransfer` y `AvgDisksecPerTransfer_Base`, con aritmética decimal
  antes de restar. `windowMs` informa la espera solicitada y `transfers` el delta
  de base válido. Frecuencia positiva e igual, contadores no decrecientes y
  timestamps crecientes cuando están presentes son obligatorios.
  Base delta cero significa idle. Reinicio, propiedad ausente o nula significa
  unknown. El contador formateado trunca segundos fraccionarios y no se usa.
- `guestRuntime.series` lista `Name` y `pct` de
  `Win32_PerfFormattedData_HvStats_HyperVHypervisorVirtualProcessor.PercentGuestRunTime`.
  Excluye nombres que contienen `_Total`, sin totales ni promedio. Un GUID no
  identifica WSL. Una consulta válida vacía es ok; clase ausente es unknown.
- `boot.bootId` concatena host y `Win32_OperatingSystem.LastBootUpTime`
  normalizado a UTC. `lastBootUtc` conserva la fecha; un fallo tiene causa
  explícita. Es identidad de observación, sin leer el registro de Windows.
  Su estabilidad ante cambios de reloj y DST no está probada como autorización
  para terminar procesos.

No hay archivos sonda, E/S sintética ni recorridos de volúmenes.
Una ventana idle no describe el resto del minuto. Una muestra de CPU puede
captar un pico.

## Avisos y persistencia

Los avisos usan `desktop-vigilante/aviso@1`, con `key`, `level`, `previous`,
`kind`, `value`, `ts`, `host` y `message`. Cambiar a warn o crit emite `enter`.
Mantener ese nivel emite `repeat` al vencer `CooldownMin`. Unknown usa
`CooldownUnknownMin` y `kind=unknown`. Volver a ok emite `recover` una vez.
Idle no avisa ni borra el nivel previo, por lo que no simula una recuperación.
Cambiar de boot o retroceder el reloj permite volver a avisar. `-NoPersist`
evalúa los avisos sin historia previa en cada ejecución.

Cada invocación persistente adquiere un mutex `Global\desktop-vigilante-<sha256>`
derivado de la ruta absoluta normalizada, sin distinguir mayúsculas ni separador
final. El mutex cubre lectura de cooldown y escrituras, y se libera en finally.
Un directorio distinto usa otro mutex. Si está ocupado, se devuelve la muestra
con `persist.jsonl=busy` sin leer ni escribir estado. El namespace Global permite
compartir el mutex entre sesiones Windows. El cruce entre sesiones distintas
queda pendiente de una prueba específica; la suite verifica procesos distintos.

Los archivos propios son los siguientes.

- `muestras-YYYY-MM-DD.jsonl` contiene muestras con fecha UTC.
- `avisos-YYYY-MM-DD.jsonl` contiene los avisos emitidos.
- `ultimo.json` contiene la última muestra, también cuando hay tope.
- `estado-windows.json` contiene `desktop-vigilante/estado@1`, el boot, niveles,
  último aviso por clave y un solo timestamp de aviso de tope.
- `estado-windows.corrupto.json` preserva una primera corrupción sin rotación.

Los dos JSONL tienen tope diario por archivo y tope acumulado del directorio
antes de cada append. Se cuentan los archivos inmediatos, incluidos ocultos,
sin recorrer subdirectorios. El directorio debe ser exclusivo del monitor.
No se borran logs ni se rota historial. Los archivos de control se reemplazan
atómicamente con temporales propios en el mismo directorio; su pequeño overhead
puede superar `MaxDirBytes` cuando ya no cabe historial. Los límites no son una
cuota rígida del sistema de archivos. Un apagado puede dejar un temporal propio;
no se purgan temporales de otras invocaciones.

Al topar, `persist.jsonl` o `persist.alerts` indica `capped` en stdout y snapshot.
El aviso de tope se limita a uno por día UTC, salvo retroceso del reloj.
Si un aviso no cabe en JSONL, igualmente sale en stderr y `persist.alerts=capped`.
El snapshot sigue fresco. Disco lleno, permisos o reemplazo fallido tienen
`persist.error`, stderr y exit 3; se intenta guardar también esa causa en snapshot.
Los JSONL, snapshot y cooldown no forman una transacción conjunta ante un crash.

Una primera corrupción se copia sin alterar sus bytes, reinicia el cooldown,
informa error y sale con 3. La invocación siguiente usa el estado reparado.
Si ya existe la copia y el estado vuelve a corromperse, no se sobrescribe ninguna
de las dos evidencias ni se añaden copias; se requiere resolver esos archivos.

## Verificación sin dependencias

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File scripts\desktop\vigilante.tests.ps1 `
    -TestRoot "$env:TEMP\pao-desktop-watch-prueba-propia"
```

Use una carpeta temporal nueva y propia en cada ejecución. La suite no usa
Pester ni CIM real. Prueba fórmulas, nulos, umbrales, cooldown, escritura atómica,
topes, corrupción y concurrencia con un proceso PowerShell propio. Conserva sus
artefactos para inspección. Un smoke real requiere una invocación `-NoPersist`
y dos con el mismo `-StateDir` temporal, sin cargar el disco artificialmente.

## Tarea programada propuesta, no instalada

No hay tarea instalada por este código. Estos comandos son sólo instrucciones
de una futura instalación después del OK de Felix, desde una cuenta limitada.
El checkout debe permanecer accesible y no depender de un worktree temporal.

```powershell
$scriptPath = 'C:\ruta-estable\personal-agent-orchestration\scripts\desktop\vigilante.ps1'
$statePath = "$env:LOCALAPPDATA\pao\desktop-vigilante"
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -StateDir "{1}"' -f $scriptPath, $statePath
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'PAO desktop vigilante' -Action $action `
    -Trigger $trigger -Principal $principal -Settings $settings
```

Cada ejecución toma una sola muestra. La sesión debe permanecer abierta.
La repetición sin duración y el comportamiento de ventanas quedan pendientes
de validar en una instalación autorizada. PAO no ejecuta estos comandos.

## Contrato futuro U3, NO IMPLEMENTADO

U3 conserva `nativos: []` hasta que una integración Windows esté validada.
El vigilante no lee ni crea registros de procesos. El contrato futuro exige
un registro `registro-nativo@1` con `namespace=windows`, `runId`, `leaseId`,
`host`, `bootIdWindows`, `pid` Windows, `creationDateUtc`, `image`, `parentPid`
y `registeredUtc`. `creationDateUtc` conserva igualdad UTC exacta con una
resolución común de microsegundos `yyyy-MM-ddTHH:mm:ss.ffffffZ`, obtenida de
la misma fuente nativa en registro y verificación.

El lanzador Windows debe crear el proceso y leer su identidad nativa. No puede
adoptar un PID arbitrario ni traducir un PID Linux. El registro debe nombrar el
lease y éste listar el mismo runId en su namespace Windows. Antes de terminar
un proceso, deben coincidir namespace, host, boot, PID, CreationDate e imagen,
además del vínculo bidireccional con un lease existente. Dato ausente o mismatch
no autoriza terminar. Un registro por sí solo no prueba orfandad; hace falta
evidencia explícita de corrida muerta o lease acabado.

Quedan pendientes las pruebas de identidad con handle retenido, sus carreras
frente a PID reutilizado y la obtención de CreationDate con offset ante timezone
y hora ambigua de DST. Este documento no garantiza que `Start-Process -PassThru`
mantenga por sí solo un handle adecuado ni que convertir un DateTime local
resuelva DST. También queda pendiente validar una identidad de boot apropiada
para cleanup. Ninguna de estas propuestas habilita terminación en esta unidad.
