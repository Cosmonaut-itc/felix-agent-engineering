# Hostgate en sombra

Implementación local Python 3.9 stdlib de `fleet/host-reservation@1`, autoridad
`f6dea9a7afd182dff9742d52784dc4e98aeb451f`, merge del PR #17
(contenido idéntico al aceptado `a1f00fa`). Mini ya aceptó la semántica.
Fleet revisará wire/IPC/PR; PAO solicita el OK concreto de Felix para activar.
Este paquete no instala ni ejecuta cargas; el endpoint sombra es manual.
`nativos=[]`. No modifica el runner, los slots ni los consumidores.

## Configuración administrativa preparatoria · SEN-3822, corte 2a

`admin.py` ofrece `local_authority()`, `load_config(locator)`, `load_anchor(locator)`
y `AuthorityLease(locator, config)`. El locator productivo es fijo: `node.json`,
`authority.json` y `authority.lock` bajo `/Library/Application Support/Fleet/hostgate`;
el socket es `/private/var/run/fleet-hostgate/ipc/control.sock`. No consulta
HOME, cwd ni variables de entorno. El CLI de consulta carga esa autoridad.

`fleet/hostgate-node@1` tiene campos cerrados y revisión `cfg_` + SHA-256 del JSON
canónico validado, incluido `operatorRevision`. Declara presupuesto contable,
volúmenes (`id -> ruta absoluta`), ejecutores Darwin/unix, uids permitidos,
principals (`uid decimal -> {roles, executors}`), perfiles y plazos. Los roles son
requester/supervisor/query; la ACL interna usa `uid:<n>`. Los perfiles contienen
`minimumDemand`; su presencia no acredita ejecución. Los mapas y listas se copian
a dicts inmutables compatibles con JSON y tuplas, sin cambiar demanda ni mínimos.
`hw.ncpu` y gethostuuid contrastan el físico; no calculan el presupuesto.

El cargador recorre componentes por `dir_fd` con `O_NOFOLLOW`, comprueba propietario,
modos, link único de archivos, ACL nativa y APFS local, también en los volúmenes.
Rechaza conservadoramente cualquier ACL ALLOW de escritura/borrado/cambio de permisos.
La ausencia de ACL exige que fstatx/filesec aporte owner, group y mode iguales a
fstat del mismo fd: fstatx puede devolver éxito con filesec vacío al fallar realloc.
No se interpreta errno tras un éxito; propiedades/entradas sin datos cierran.
Configuración/ancla tienen límites de 64 KiB/4 KiB y parser JSON estricto.

`fleet/hostgate-authority@1` fija hardware/host/store/stateDir/modo, separado de la
política. El lease compara esa vinculación antes de abrir SQLite, toma flock no
bloqueante sobre el lock preexistente y valida `stateDir/shadow.db` existente sin
inicializarlo. Conserva fds y vigila dev/inode de la cadena, lock y ledger; `check()`
rechaza revisión, permisos o pathnames alterados. El dispatch llama `check()` antes de cada operación. Fallos administrativos llevan exit 6; lock ocupado usa
`Busy` (exit 4). B, incluso con storeId clonado, no sustituye a A mediante node.json.
Reducir presupuesto sobre A conserva reservas/exclusivos y permite disponible negativo.
El servicio no provisiona ancla/lock/store ni habilita Fleet o cargas.

Las pruebas privadas usan archivos propios dentro de la copia remote-run. Sustituyen
explícitamente owner/grupo y la exclusión de rutas de checkout en una seam privada;
la validación productiva de `NodeConfig` conserva esa exclusión. No acreditan separación
root/cliente instalada ni defensa frente a un actor hostil con escritura al ledger.
`tests/support/world.config` aplica esa política también en cada hijo del harness;
los mocks del proceso padre no sobreviven a exec.
Los valores operativos de la Mini siguen pendientes de confirmación de Felix.

En modo sombra, toda respuesta pública lleva `result: shadow`, `wouldGrant`, `operationId`,
`configRevision`, `exitCode` y `reasons: [{code,detail,retryable}]`. No hay permiso
ni `granted` efectivo. `simulationKey` es exclusivamente diagnóstico de la BD
`shadow.db`: nunca promoverla, copiar sus generaciones a un ledger efectivo ni
usar sus reservas para ejecutar. El estado interno `granted` de una preparación
sólo significa que terminó la simulación de su commit (sketch 25). `estado` lo
publica como `simulated` tanto en `state` como en `effectiveState`; el literal
`granted` queda exclusivamente dentro del ledger.

Desde `fleet/`:

```sh
python3 -B -m hostgate reservar < solicitud.json
python3 -B -m hostgate estado < consulta.json
```

Sin endpoint autenticado estas entradas rechazan con exit 5. No se lee configuración
ni identidad de payload/variables de entorno; no hay flags de pruebas o rutas de
estado elegidas por el cliente. El CLI de sombra completo se usa mediante el harness
local `cli.main(argv, gate=Hostgate(config, mundo), auth=contexto, stdin=..., ...)`.
`tests/support/world.py` muestra todas las fronteras inyectadas, no una interfaz
remota. `prepare` sólo modela preparación interna, sin presupuesto, cola ni renovación
Fleet. El harness acredita roles, perfiles, canales y evidencia **sintéticos**.

La configuración `NodeConfig` se construye en la frontera administrativa de confianza,
fuera del JSON. Declara host/store/ejecutores, revisión, presupuesto, mínimos por perfil,
recursos canónicos, ACL, rutas, TTL, plataforma por ejecutor y drenado inicial del host.
La plataforma se registra en la reserva; el kind de la evidencia no la decide.
El drenado administrativo es persistente y una consulta no lo reabre. `ShadowStore.initialize` exige directorio
nuevo privado fuera del repo; es una acción administrativa explícita del harness.
Abrir un ledger ausente, corrupto, de otro store/schema/modo o con symlinks nunca lo
recrea. El despliegue deberá acreditar filesystem local persistente, ownership,
ACL y configuración no escribible por solicitantes; el paquete no infiere eso del
nombre de una ruta. Los temporales de las pruebas no son rutas de producción.

Solicitud de reserva exacta:

```json
{"schema":"fleet/host-reservation@1","operationId":"op1","operation":"reservar","jobId":"j1","attemptId":"a1","executorId":"unix","physicalHostId":"node","demand":{"cpuMilli":1000,"memoryBytes":1000,"diskBytes":{"data":1000},"slots":1},"exclusiveResources":["postgres:5434"],"profileId":"fixture"}
```

CPU en millicores; memoria/disco en bytes; slots enteros. Todos rechazan booleanos,
negativos, floats, no finitos y dimensiones no registradas. Los mínimos del perfil
se aplican antes del presupuesto. Recursos son nombres canónicos del catálogo;
`sen-slot:1/2`, `sen-test-db-1/2`, puertos 5434/5435 y BD por corrida deben registrarse
como recursos del mismo presupuesto. Los dos slots SEN no se duplican.

Los otros envelopes llevan las tres claves comunes anteriores:

| operation | Campos adicionales exactos |
| --- | --- |
| latido | `key:{storeId,reservationId,generation}`, `seq` |
| dudosa | `key`, `reason`, `evidenceIds:[]` |
| soltar | `key`, `evidenceIds:[]` |
| estado | ninguno |

JSON acotado a 64 KiB, sin duplicados/NaN/NUL/campos desconocidos. Una línea JSON
stdout; diagnóstico genérico stderr sin payload/secretos. Exit: 0 sombra/consulta
confirmada; 1 rechazo; 2 uso/conflicto; 3 commit incierto; 4 busy; 5 no autorizado;
6 estado/entorno inutilizable; 130 interrupción sin prueba de fin. El catálogo cerrado
de motivos está en `protocol.CODES`; conserva detalle original de salud/dimensión/recurso.
La allowlist de umbrales sólo clasifica: no autoriza degradación, cuya política no
está acordada. Un motivo nuevo siempre es `health-invalid`. La selección por carga,
la cola y el veto auto son de Fleet, no se implementan aquí.

El ledger sombra usa schema interno 2; no migra ni promueve ledgers previos.
SQLite WAL/FULL serializa `BEGIN IMMEDIATE`: vista de estado, presupuesto, exclusivos,
contador por host/job, operación idempotente y auditoría se confirman juntos. La vista
compacta con checksum evita repartir escritores y tablas sin caller. No hay transacción
con recursos externos. Un fallo/ACK perdido de commit devuelve exit 3, `wouldGrant:false`;
se relee **la misma operación** sin compensar ni repetir creación. Replay lee la
operación y compara el contexto (peer/ancla/canal) antes de revelar `originalResult`
y `currentState`, siempre con `wouldGrant:false`. La autorización de una preparación/reserva se
comprueba antes de insertar su resultado: el rechazo de un peer/canal ajeno
confirma sólo la proyección conservadora y su auditoría, sin ocupar el `operationId`
del dueño ni aplicar sus cambios. Un fallo de proveedor dentro de la transacción
confirma sólo la proyección conservadora, incluida la segunda revalidación y el
drenado, y su audit; devuelve exit 6, sin aplicar la mutación ni ocupar `operations`.
Si la escritura o el commit de esa confirmación fallan, devuelve exit 3. `prepare`
sigue la misma regla de persistencia; su interfaz interna relanza la excepción
original después de confirmar el audit con exit 6, y propaga el fallo de
persistencia si la confirmación falla.
Un conflicto de payload en `prepare` confirma la proyección y el audit con
`operation-conflict`, exit 2, y después relanza `ValueError('operation-conflict')`;
conserva la operación original sin crear otra preparación.
La vista, el resultado/audit y el contexto de una operación nueva se serializan
dentro de la guarda conservadora; el store escribe esos mismos textos JSON
sin volver a serializar. El contexto de autenticación se captura una sola vez
por llamada, también para comparar un replay
([SEN-3952](https://linear.app/mdr/issue/SEN-3952)): valores de proveedor como
`nan`, `inf` o `bytes` conservan la duda y el audit, sin normalizar esos valores
ni aplicar la operación fallida. Rigen exit 6 y exit 3, y el contrato de excepción
de `prepare`, descritos arriba. La protección incluye `reservar`, `prepare`,
`latido`, `dudosa` y `soltar`, y resultados sin operación registrada o de rechazo ajeno.
Un fallo al confirmar un rechazo ajeno sigue la tabla de errores
del almacén; nunca se informa exit 5 con esa escritura en duda. Un rechazo histórico no se vuelve admisible al mejorar
salud; un cambio de payload da `operation-conflict`. Fencing usa la terna completa.

`estado` abre sólo lectura, separa `state` y `effectiveState`, y muestra presupuesto
por estado/dimensión, recursos, ancla, identidad y edad del latido. No escribe, renueva,
libera o reabre. La próxima mutación, incluida `prepare`, persiste expiraciones/dudas
antes de decidir.
El contenido durable (BD, WAL con cabecera o frames y vista lógica) debe conservarse;
un WAL ausente equivale a uno de cero bytes creado por SQLite al abrir sólo lectura.
El archivo auxiliar `shadow.db-shm` queda fuera de esa comparación.
Preparaciones con EOF, muerte, plazo o identidad desconocida terminan con lápida; no
reviven por una operación nueva del mismo huérfano. Reserva retiene desde commit,
incluso si el dueño muere inmediatamente después de revalidación. Boot/reloj/latido/
control dudosos conservan toda la demanda y exclusivos. Heartbeat fresco no reconcilia.
Pausa, root exit, salud sana, desaparición de slots/BD o restart WSL tampoco liberan.
Cambiar boot drena el físico; reapertura/reconciliación positiva queda pendiente.
El rol `operator` no puede ejecutar `dudosa` ni `soltar` sobre la reserva de otro
supervisor: ambas exigen al dueño exacto y su canal. Sin supervisor, la reserva
queda `uncertain` hasta recuperación explícita. Esa recuperación es F, pendiente
de SEN-3834, SEN-3835 y SEN-3836; no hay excepción administrativa implementada.

`ProcessTable` consulta sólo un PID por vez: libproc/sysctlbyname mediante ctypes en
Darwin, `/proc` con ticks exactos y boot antes/después en Linux. EPERM/lectura parcial
es `unverifiable`; PID reutilizado/zombie es ausencia/diferencia del solicitante, nunca
prueba de cierre de ámbito. No enumera ni mata. `ProcessTable` iguala hoy el boot
físico al del ejecutor; esa limitación debe resolverse antes de cablear WSL, cuyo
boot no acredita el de Windows. No hay integración WSL implementada. ABI/clock en Mini aún requieren una
aceptación específica de plataforma: el test de identidad puntual es evidencia acotada.

Desde 4c-1, `authorize_creation`, `consume_creation` y `record_started` son métodos de
`Hostgate` en modo efectivo. Desde 4c-3 viajan por el wire (sección «Autoridad de
creación»); Fleet sigue sin llamarlas.
`ScopeVerifier` real nunca acredita un perfil/cierre. Para Unix futuro se exige sesión/
pgid propio **más linaje con procedencia y completitud** bajo perfil auditado que impida
setsid/cambio de grupo, doble fork o delegación no controlada. Registrar comandos,
wrappers, runtime/versiones y evidencia aceptada es parte de esa acreditación. Raíz
zombie, subreaper o linaje muestreado no bastan; un proceso único también necesita prueba.
Cgroup requiere delegación/contención previa; WSL no acredita boot o árbol Windows.
Windows sólo aceptará empty/never-created del launcher propio validado, no exit0/TTL+5.
El ledger conserva la evidencia positiva sintética con emisor, key, boot, ámbito,
procedencia, completitud, limpieza y digest en la transacción del cierre.
Las pruebas `ClosureEvidence.fixture` modelan estas condiciones y limpieza completa;
los negativos alteran una garantía y recalculan su digest. La reserva Windows del
harness es explícita; ni una prueba Windows libera Unix ni una Unix libera Windows;
no son evidencia nativa. `soltar` incompleto retiene todo; no hay liberación parcial.

`AuthContext` sólo entra como objeto de la frontera de confianza en proceso, nunca
como JSON. El servicio futuro deberá verificar peer kernel conectado por el supervisor,
o canal heredado más identidad del hijo acreditada; un socketpair del creador no
atribuye automáticamente sus credenciales al hijo. ACL/principal autorizan roles:
TTY, nombres, IDs, nonces visibles o texto de OK no dan autoridad. El CLI no implementa
ese transporte ni finge autenticación con PPID. Misma cuenta OS con escritura del runtime/
BD puede alterar el estado; no se promete aislamiento hostil. Separar cuenta es decisión
de provisión, no requisito oculto para probar sombra.

`salud/lector_desk.py` adopta la copia corregida y aceptada de Desktop (registro r4 abajo). Sus 112 casos/aserciones originales
se ejecutan con parametrización stdlib en `tests/support/cases.py`. `lector_mini`
implementa el contrato U4; recibe boot independiente por parámetro. Usa la ventana
[-60,180] s de B, propuesta del consumidor que U4 no especifica y debe aceptar Fleet.
La identidad del host y ncpu se contrastan con configuración. Sin SEN-3853 el desk no
es sano ni degradado; DefaultShell pendiente/fallido implica cero llamadas al transporte.
No se implementa transporte. Ninguna consulta dispara sondas ni intenta obtener testigo.
A-M1 queda resuelto por la adopción del lector Desktop corregido. La captura de
`OverflowError` en el adaptador se conserva como defensa en profundidad.

`owner_record.py` propone `fleet/host-owner@1`, con supervisor exacto separado de raíz,
key/correlación/boot/started/secuencia. Publica temporal privado, fsync, replace y fsync
del directorio. Registro incompleto/ilegible protege; root exit con hijo conserva ended=null.
Publicar fin real falla cerrado hasta proveedor/aceptación U1. U1 actual sólo conoce
corrida/slot y puede declarar muerte por exit; no se copian esas reglas. U2 sólo legado.

Migración futura: sombra → cerrar/drenar entradas y reconciliar → adaptar `run.sh` a
Fleet → acreditar rechazo de copias/entradas directas antiguas → abrir Fleet con OK.
No se engancha el runner en esta entrega ni se declara probado el corte real. Lápida
cliente o censo no prueban exclusividad. Commit ambiguo conserva recursos/directorios.
SEN conserva dos slots hasta adaptación coordinada. Un restart WSL requiere drenado,
inventario de afectados, reconciliación y reapertura explícita.

Pruebas exclusivamente en Mini mediante el `run.sh` de comprador y su turno:

```sh
cd fleet
/usr/bin/python3 -B -m unittest discover -s hostgate/tests -t . -v
```

Los fixtures Jale son **reproducciones modeladas**, no la captura original de las 22:54Z:
tres preparaciones sin reserva, PID/inicio/ancla/canal/latido, lápidas, replay sin adquisición,
seguidor y variante que pudo crear. El fixture ligado y el legado son distintos; además
tres helpers reales salen por EOF al terminar sus padres, y tres legados sobreviven
y reintentan contra el motor. Ambas variantes incluyen el caso que pudo reservar:
retiene aunque quede presupuesto. Los helpers tienen barreras y salida acotada.
La concurrencia SQLite es real entre clientes del API; IPC/supervisor/launcher/profiles/
adaptador real y aceptación U1 quedan futuros. No se valida activación con estos fixtures.

La tabla [COBERTURA.md](COBERTURA.md) registra requisitos, límites y estado real
por prueba. Actualización fechada 2026-10-23 (America/Mexico_City): las dos
primeras corridas se conservan como fallidas, ambas con 174 pruebas y cero errores.
En `72a6d10` falló el conteo de Jale; en `731d866` Jale pasó y falló el supuesto de
orden fijo en commits concurrentes. `45b0e39` corrigió esa aserción. Una invocación
anterior terminó esperando turno, sin ejecución remota ni run_id.

La tercera corrida, run `20261023-192300-c75aef`, probó exactamente
`dad5ef88158daffe76a2a47566fec4924502da5d`: 174 pruebas, 0 fallos, 0 errores,
0 omitidas. Incluyó el rojo retrospectivo de B-G1/B-G2/B-G3 sobre `3f8ce69` en la
misma corrida. Evidencia completa:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/9df0741daa4d4a629ac47bf2c4b6be5a/lanzador.log`.
Esa suite verde acredita ese SHA, incluida la aserción corregida; no equivale a
aceptación técnica: las revisiones posteriores identificaron A-I1 y los puntos
de esta ronda.
El guion descubre las seis familias CLI, contrato, identidad/ámbito/U1,
desk (112 originales + contraste), Mini y almacén. El guion r3 sustituyó el rojo retrospectivo anterior por las dos regresiones A-I1
sobre `dad5ef8`, copiando sólo las pruebas nuevas al snapshot temporal de la base.

Corrida r3, 2026-10-23 (America/Mexico_City): run `20261023-204639-a55097`, SHA
`147ff527d04b6f622bce940ea27ed999520443e7`, árbol limpio, Python 3.9.6 en Mini
macOS 27 arm64. El turno llegó tras unos 63 minutos. Descubrimiento: CLI 9,
contrato 35, identidad/ámbito/U1 11, desk 113, Mini 5 y almacén 15.
Resultado: **188 pruebas, 0 fallos, 0 errores, 0 omitidas**. En la misma corrida,
las dos regresiones A-I1 fallaron sobre `dad5ef8` (2 fallos, 0 errores, ambos
`5 != 0` al reintentar el dueño) y pasaron sobre el SHA corregido. El replay B-G2
y el rechazo durable con salud recuperada también pasaron.
Evidencia completa:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/495ef06ee60541bd8626124333b1b7f4/remoto.log`,
con `lanzador.log`, `script.sh`, `recibo.json` y `terminal.json` en ese directorio.
Una corrida de las dos permitidas; no se usó la segunda. Lanzador terminado y
turno liberado, salida 0. El commit posterior sólo actualiza README y COBERTURA;
no se presenta como otro SHA ejecutado. Suite verde no equivale a aceptación
técnica de Fleet, integración ni activación.

El contraste `test_observacion_powershell_contraste_d8fecdc7` usa las seis variantes
del productor Desktop `d8fecdc7`, con source y scope exactos. La observación no cambia
motivos del lector ni expone conteos, tampoco convierte lower bounds o null en salud.
El corpus desk aceptado se conserva; la adopción autorizada r4 se registra abajo.

La observación de identidad/canal se limita a preparaciones `validating` y reservas
no liberadas. No se borran lápidas ni operaciones. SQLite aún ejecuta `quick_check`
al abrir y reescribe la vista completa por mutación: ese coste crece con el historial,
no está medido y cualquier conservación/compactación requiere acuerdo. Los fallos FULL/I/O se inyectan
en la frontera SQLite y los fsync del registro antes/después de replace; no se
acredita daño físico real ni un fallo del fsync interno de SQLite a nivel VFS.
ABI/IPC, perfiles reales, consumidor U1, corte real, DefaultShell, SEN-3853 y
recuperación explícita permanecen pendientes. Ningún fixture acredita activación.


Adopción A-M1, 2026-10-23 (America/Mexico_City): copia byte a byte de
`/Users/felixddhs/.claude/encargos/desktop-carga/lector-numero-am1/artifact/lector_desk.py`,
Desktop `d21312b1`, empaquetado en `ca44846d`. Blob verificado antes del commit:
`1ef0465d847836129dc155a80bf34fac8e169a4a`. Desktop acreditó en Mini los runs
`20261023-205755-d82822` (112/112 en base y corregido; regresiones 22 fallos en base
y 36/36 en corregido) y `20261023-210507-aedc03` (113/113 del corpus hostgate en ambos).
El corpus `test_lector_desk.py`, `support/cases.py` y `fixtures/health/desk.json`
conserva sus blobs. `test_lector_desk_adoptado.py` añade dos casos de JSON ±10**400
por el adaptador, con control sano, degradación y rechazo; se mantiene la captura
de desborde de `admission.py`. No queda pendiente A-M1 para Desktop. La evidencia
de ejecución de esta adopción se registra con la corrida r4.


Correcciones r4, 2026-10-23 (America/Mexico_City): `ForeignOwner` separa autorización
de `OSError`/`PermissionError`; fallos de proveedores previos a escritura salen 6.
SEN-3900 añade la confirmación de la proyección conservadora y su audit ante esos
fallos; si la escritura o el commit de esa confirmación fallan, la salida es 3.
Esto también rige para `prepare`, que conserva la señal de excepción de su
interfaz interna.
El almacén conserva proyección y auditoría ante rechazo ajeno, sin insertar la
operación. El espacio de `operationId` es por principal: un peer puede ocupar con
operaciones propias ids que A todavía no usó. Una preparación existente protege
su id contra otro peer/ancla/canal y contra otra operación del propio dueño
(`operation-conflict`, exit 2 sin ocupar `operations`).
`WriteUncertain` comunica fallo de escritura SQLite o de commit/ACK: toda excepción
en commit o después sale 3 y exige releer la misma operación, sin compensar.

El guion r4 adapta el de r3 en `tests/mini-r4.sh`, dentro del único subárbol
autorizado. Usa rojo retrospectivo A2-I1/N-2 sobre `c23cc0f`, seis familias y
exactamente 113 pruebas desk. Los dos casos nuevos del adaptador desk se cuentan
en la familia contrato, además de descubrirse por unittest. Sólo tolera en
porcelain la línea exacta `?? .remote-run.lock` (SEN-3885). Las pruebas se escribieron
antes del código corrector; su ejecución se registra abajo.


Ejecución r4, 2026-10-23 (America/Mexico_City): run `20261023-211240-fc0f10`,
SHA probado `1c3aef683a7df5df704fa5910be06a8f7d549a30`, incluida la adopción
Desktop `98a7bc1`. Árbol limpio y congelado durante espera y ejecución; sólo
se toleró la línea exacta de SEN-3885. Mini con turno, `/usr/bin/python3` 3.9.6,
macOS 27 arm64. Una corrida de dos permitidas; no se usó la segunda.

| Familia | Descubiertas / ejecutadas | Resultado |
| --- | --- | --- |
| CLI | 10 | PASS |
| Contrato + adaptador desk adoptado | 41 (39 + 2) | PASS |
| Identidad/ámbito/U1 | 11 | PASS |
| Desk | 113 (112 originales + contraste) | PASS |
| Mini | 5 | PASS |
| Almacén | 18 | PASS |
| Total | 198 | 0 fallos / 0 errores / 0 omitidas |

Rojo retrospectivo sobre `c23cc0fe6feda40b5383670554910011d303f4d2` en la
misma corrida: las cuatro pruebas A2-I1/N-2 fallaron, con 8 aserciones fallidas
(incluidos subtests), 0 errores. Se observaron `reserved` en vez de `uncertain`,
`validating` en vez de `expired`, consumo del id ajeno y latido del dueño en vez
de conflicto. Las cuatro pasaron en el SHA corregido. A-I1 de r3 y replay B-G2
permanecen intactos y pasaron. También pasaron PermissionError de proveedor y
symlink, commit/ACK/cierre con OSError/PermissionError/RuntimeError y recuperación
con un solo débito, además de JSON ±10**400 por el adaptador de salud.

Evidencia completa, sin recortes:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/a746e9d92e33413eb68e93a22172e296/remoto.log`.
En ese directorio: `lanzador.log`, `script.sh`, `recibo.json` y `terminal.json`.
Recibo con `evidencia_completa=true`; resultado, start y salida del lanzador 0;
turno liberado. Lanzador terminado; helpers propios concluyeron en la suite.
No hubo pruebas ni imports del candidato en MacBook, conexiones al desk, push ni PR.
El guion se versionó dentro de `fleet/hostgate/` en vez de editar el externo
`evidencia-hostgate-sol/guion.sh`, para respetar el subárbol exclusivo autorizado.
La evidencia externa la escribió el runner autorizado. Después de la corrida
sólo cambian README y COBERTURA; el head documental no se afirma ejecutado.
Suite verde no equivale a aceptación técnica de Fleet ni activación. Siguen F
ABI/IPC, supervisor, perfiles reales, consumidor U1, corte de run.sh/mini.lock,
DefaultShell, SEN-3853, recuperación/reconciliación explícitas y medición del coste
con historial largo. A-M1 de Desktop ya está resuelto por la adopción autorizada.

SEN-3822, corte 2b: `darwin_peer.DarwinPeers` añade observación local optativa,
con `getpeereid`, audit token, PID/nacimiento/uniqueid/pidversion y bootsession UUID.
`authenticate` realiza tres capturas, registro confirmado de NOTE_EXIT y reto
simétrico fresco de 256 bits (frames privados de hasta 64 KiB; deadline total 5 s).
`alive` exige el objeto de socket original, recaptura y comprueba salida/EOF;
`verify` entiende exclusivamente el token extendido de cuatro enteros. El llamador
posee los sockets; `close` cierra los watches. No se cablea al CLI ni al runtime.
El supervisor debe conectar personalmente después de exec: Darwin puede actualizar
last_pid al escribir por un fd heredado; una identidad ya autenticada no se adopta
como otro escritor. No se acredita aislamiento ante transferencia hostil del fd.

`DarwinHealth` fija UUID/boottime y rodea cada lectura única de snapshot con ambas
observaciones. Produce `snapshotHost/fecha-UTC` por aritmética entera para el lector
Mini actual; cada cambio se enclava antes de la siguiente operación que pueda fallar
y cierra salud hasta reinicio, sin aprender el boot del snapshot.
La primitiva hardware UUID/configuración/ancla/lease pertenece a 2a. Pairing,
saludo de autoridad, revisión/ACL y servicio sombra quedan para 2c. No hay RPC de
reservas, servicio instalado, permisos efectivos ni cambios al productor/lector.

SEN-3822, corte 2c: arranque manual `python3 -B -m hostgate.service`, únicamente
con configuración/ancla/ledger ya provisionados. `HostgateClient.connect()` consulta;
`request_supervisor` ofrece una solicitud y exige aprobación recíproca R↔S antes
conectar un supervisor mutador. Cada reservar queda ligado al digest original;
EOF de R después del grant permite latidos de S; perder S conserva capacidad.
El CLI `estado` usa el endpoint fijo; fallo de transporte sale 6 sin stdout parcial.
`wire.py` limita entradas a 64 KiB y transmite respuestas completas mediante DATA,
END (uint64/SHA-256) o ERROR. Spool privado hasta 1 MiB en memoria, límite local
ampliable de 256 MiB; no hay truncado, compensación ni reconexión de propietario.
No instala launchd ni activa Fleet, perfiles, cargas, U1 o el legado. Pruebas:
`tests/mini-ipc.sh`, sólo con remote-run en Mini y fixtures privadas dentro de la copia.

El corte aceptado de este PR es **2c-I: endpoint, pairing, framing y cliente**;
la campaña integrada de autoridad y fallos corresponde a **2c-II**.
La consulta elige un ejecutor de la ACL del uid efectivo local, a partir de la
configuración validada; si no hay ninguno, rechaza explícitamente antes de conectar.
H conserva la autorización del peer. Cancelar la llamada o el pairing cierra la
sesión y produce `TransportError(outcome_unknown=True)`, sin compensar reservas.
El CLI `estado` por IPC sale 6, con diagnóstico en stderr y stdout vacío al cancelar;
el harness histórico con `gate/auth` inyectados conserva su salida 130.
Las regresiones de R1-W1/R1-W2 están en `test_ipc.py` y `test_cli.py`;
`mini-ipc.sh` las ejecuta junto a las tres familias dirigidas en ambos stacks.

SEN-3822, 2c-II: el endpoint usa I/O no bloqueante y buffers de entrada/salida por
conexión (`ipc_io.Channel`). `select` avanza como máximo una lectura/escritura por
canal listo; un frame parcial o lector detenido no monopoliza el servicio.
Capturas, watches, sesiones, `_decision`, proyección, replay y store se ejecutan
serialmente en el mismo hilo. El handshake conserva 5 s; las operaciones conservan
30 s de inactividad y 300 s totales. Cerrar un canal nunca compensa su reserva.
La dirigida Mini es `tests/mini-ipc-progress.sh` (CX-2, TTL real de 10 s) y
`tests/mini-ipc-campaign.sh` (campaña integrada y vecinas seleccionadas), ambos
con Apple Python 3.9.6 y Python 3.14.7, temporales privados y sin instalar servicio.
El estado sigue en sombra; PR 3 espera la revisión APTO y la suite del orquestador.


## Reserva efectiva preparatoria · SEN-5649, entrega 3a

`NodeConfig.mode` admite `shadow` y `effective`; el ancla debe coincidir exactamente.
`store_for(config)` selecciona `ShadowStore` o `EffectiveStore`, que comparten el
motor SQLite. El segundo nace vacío en un directorio inexistente con un storeId
nuevo, archivo `effective.db`, application_id 1212633925 y metadata `(storeId,2,effective)`
(versión 1 hasta 4b-1).
No importa ni promueve sombra. El runtime sólo abre estado preexistente y valida
cabecera, metadata y relaciones; ancla/configuración se comparan antes de SQLite.
La sombra conserva su archivo, metadata y forma de respuesta anteriores.

El wire efectivo conserva `fleet/host-reservation@1`: `result: effective`, `mode`,
`storeId`, `granted`, y cuando hay reserva, `reservationKey` y `state`. Su id empieza
con `e_`. No contiene campos de simulación. El replay devuelve `granted: false`,
`replayed: true`, `originalResult` y `currentState`: nunca otra concesión ni generación.
`estado` conserva `granted` en las preparaciones efectivas. Las keys de sombra o de
otro store se rechazan con `stale-generation` sin cambiar la reserva vigente.

Consumidor: `HostgateClient.connect(handoff=None, mode='shadow')` y
`request_supervisor(request, mode='shadow')`; 3b debe pedir `mode='effective'`.
El cliente contrasta modo, store y revisión con autoridad y hello; en efectivo valida
además cada respuesta. La ruta de sombra del cliente no cambia. Desde 4c-3 el CLI `estado`
toma el modo de la configuración validada.
`call(request, *, max_response_bytes=256 * 1024**2, timeout=300)` acepta sólo plazos
numéricos `0 < timeout <= 300` segundos; un fallo cierra el canal y conserva duda.
`DarwinPeers.observe_process(executor, pid)` devuelve la `Identity` nativa observada.

Desde 4c-1, `soltar` efectivo sella lo nunca emitido y cierra lo registrado con prueba
propia; `ClosureEvidence.fixture` ya no libera. Desde 4b-1 la validación admite filas `running`,
`released` y `launchPossible=True` sólo con las formas de «Formas del permiso y
evidencia real»; desde 4c-1 las escriben los métodos de creación. Desde 4c-2 el runtime efectivo
conecta la coalición y acredita perfiles. No hay CLI de provisión efectiva, mantenimiento periódico, instalación
ni activación de hosts. La espera autónoma y la integración Fleet pertenecen a
entregas posteriores. Las pruebas nuevas usan sólo autoridades y procesos privados.

El receptor DATA también fijaba `shadow`, aunque no figuraba entre los módulos del
diseño 3a. `wire.receive_response(..., _mode='shadow')` ahora contrasta el modo
verificado que le pasa el cliente; framing, checksum y END conservan su forma.


## Lector de coalición · SEN-5650, entrega 4a

`darwin_coalition.DarwinCoalition` lee, sin privilegios, la coalición de recursos
de Darwin (índice 0 del sabor 20 de `proc_pidinfo`). En 4a nada del runtime de hostgate
ni de Fleet lo importaba. Desde 4c-2 `service.py` lo construye en modo efectivo con import
diferido. Cargar el runtime no lo carga y Fleet no lo importa; una prueba lo comprueba.

- `certify(owner: Birth) -> Baseline`: barrido aceptado de la coalición del dueño.
  La línea base guarda boot, coalición, todos los miembros con `Birth`
  `(boot, pid, uniqueid)`, uid, ruid, svuid, `parent_unique` y `pidversion`, los
  contadores `(tasks_started, tasks_exited)` y un digest. Es inmutable y su JSON
  canónico se relee estricto. Más de 256 miembros se rechaza, no se recorta.
  Un barrido no aceptado sale como `CoalitionUnavailable` con `Retry` o `Invalid`.
- `observe(baseline)` devuelve una de cuatro variantes. `Complete`: ningún miembro
  vivo fuera de la línea base. `Occupied`: miembros nuevos, cada uno con
  credenciales y `elevated` si euid o svuid difieren de ruid. `Retry`: contadores
  distintos, cardinalidad discordante, lista saturada, identidad cambiante entre
  dos lecturas o más de 50 ms. `Invalid`: boot o coalición perdidos, contadores
  incoherentes, tamaño inesperado o un error de lectura distinto de `ESRCH`.
- `observe_root(owner, pid) -> RootIdentity`: hija directa del dueño, en su
  coalición, leída dos veces entre dos lecturas de boot; si no, `RootRejected`.

Un barrido se acepta sólo con los dos contadores iguales antes y después, lista
sin llenar el buffer, `uniqueid` igual antes y después de leer coalición y datos
cortos (sabor 13), boot estable y tantos miembros legibles como started − exited.
`ESRCH` omite un candidato sólo bajo esa concordancia. `observe` no amplía la
línea base ni descuenta una novedad por su padre, nombre, uid o sesión.

No cubre trabajo encargado a un servicio que ya existía (launchd, Docker,
simulador), procesos con privilegios ni cambios de coalición con privilegios.
Dentro de `remote-run` la coalición es compartida: el positivo `Complete` de una
carga sólo se acredita desde una conexión ssh propia (`OwnCoalition`).


## Formas del permiso y evidencia real · SEN-5650, entrega 4b-1

En 4b-1 nada de esta entrega estaba conectado. Desde 4c-1 los métodos de creación de
`Hostgate` escriben estas formas y `soltar` las sella o las cierra.

Una fila efectiva lleva, opcional, el registro `creation`
(`darwin-coalition-creation@1`); `scope.creation_from_row` lo lee como una variante:
`Unissued` (sin registro), `Issued`, `Consumed`, `Registered` (con raíz y recibo),
`Closed` (raíz, recibo y `closedBy`) o `Sealed` (`closedBy`, con o sin permiso).
Combinaciones legales: `reserved` con `Unissued`, `Issued` o `Consumed`; `running` con
`Registered`; `uncertain` con cualquiera de esas cuatro; `released` con `Closed` o
`Sealed`. `launchPossible` es verdadero exactamente con `Consumed`, `Registered` y
`Closed`. El permiso coincide con dueño, canal y boot físico de la fila; su línea base
es la `Baseline` canónica de 4a, con digest, contadores y coalición repetidos. La raíz
está en el mismo boot y coalición y fuera de la línea base. Cualquier campo de más, o un
registro que no se relee idéntico, invalida la vista entera.

`ClosureEvidence` gana `observation` (opcional; una ausente no entra en el digest ni
en la fila, así que la sombra escribe la misma forma que antes).
`closure_reasons(evidence, reservation, *, mode)` toma el modo de la configuración,
nunca del prefijo de la key; ante una fila o una evidencia ilegible retiene y no lanza. En `shadow` sólo vale `simulation` sin observación, y una
fila de sombra no lleva `creation`. En `effective` sólo vale `darwin-coalition@1` con
emisor `hostgate:<storeId>:<epoch>` y una observación del mismo proveedor,
correlacionada campo a campo con la fila y su permiso: epoch, boot, revisión del
perfil, permiso, digests, coalición y línea base. `unix-scope-empty` exige además la
raíz registrada, un barrido concordante de enteros, no anterior a los contadores de la
línea base, y no más vivos que miembros de ella; `never-created` exige raíz y barridos nulos. `closedBy` es el digest de la
evidencia. Una fila `released` sólo valida con una evidencia así.


## Acreditación, store v2 y selfcheck · SEN-5650, entrega 4b-2

En 4b-2 nada de esta entrega estaba conectado. Desde 4c-2, sólo `service.py` llama a
`accredit` y al selfcheck: el runtime efectivo corre el selfcheck una vez por arranque
si algún perfil trae bloque, y `Runtime.profile_verified` devuelve el `Accredited` de
este boot. `authorize_creation` exige que el `launch` declare el `bootstrapDigest` y el
`launcherDigest` de ese `Accredited`. Una prueba comprueba que sólo `service.py` los llama.

`admin.load_config` acepta en cada perfil, junto a `minimumDemand`, un bloque de
acreditación estricto: `scopeMechanism` (sólo `darwin-coalition@1`, el lector de
4a), `darwinBuild`, `bootstrapDigest`, `launcherDigest`, `workloadUid` y
`accreditation = {campaignRunId, sha, darwinBuild}` con el mismo build. El bloque
entra en la revisión del perfil (`AdminConfig.profiles`); un perfil sin bloque
conserva el `content_digest` de hoy. `admin.accredit(profile, *, darwin_build,
workload_uid, selfcheck)` es pura: devuelve `Accredited` o el motivo
`profile-unverified` con su detalle (sin bloque, selfcheck fallido o ausente, build
o uid distintos). El nombre del perfil no acredita nada.

`EffectiveStore` está en la versión 2. Un store efectivo en versión 1 se rechaza
con `schema` al leer la cabecera, antes de abrir SQLite: el archivo queda intacto y
no aparecen archivos nuevos. No hay migración. `mutate` valida la vista nueva antes
de escribirla, así que una fila inválida nunca se escribe.

`coalition_selfcheck.selfcheck()` comprueba el lector contra el kernel real, sin
privilegios ni launchd: boot y build, lecturas propias, línea base, un hijo
bloqueado como raíz, `fork`+`exec`, un huérfano con doble `fork` y `setsid` que no
es raíz, un zombi que no cuenta como vivo, la coalición de launchd y, al final,
ninguna identidad propia viva. Devuelve `SelfcheckPassed(boot, darwin_build)` o
`SelfcheckFailed(step, detail)`; cualquier excepción o plazo agotado es un fallo, y
todo proceso que crea termina con él. La limpieza intenta cada paso aunque falle
otro y nunca termina un pid sin revalidarlo. Si un paso o una identidad fallan,
el resultado es `SelfcheckFailed('cleanup', 'limpieza incompleta: ...')`.


## Autoridad de creación · SEN-5650, entregas 4c-1 a 4c-3

Tres operaciones nuevas del wire, sólo del supervisor dueño en el canal de la reserva y
sólo en modo efectivo. En sombra validan la petición y responden
`enforcement-unavailable` con exit 5. El CLI las nombra, pero sin emparejar responde
`enforcement-unavailable` sin escribir en el store. `estado` toma el modo de la configuración
validada.

- `authorize_creation {key, confirmation, launch}`. `confirmation =
  {operationId, requestDigest, attemptId, source}` repite la admisión que dio la reserva
  (`source` es `reply` o `same-channel-replay`). `launch = {argvDigest, inputDigest,
  bootstrapDigest, launcherDigest}`. Exige el permiso `Unissued`, la generación vigente y el perfil
  acreditado por el runtime para el uid del supervisor, con los dos últimos digests iguales a
  los del bloque que lo acreditó. Certifica la línea base de la
  coalición del dueño y emite el permiso `Issued` con `pmt_<hex>`, el epoch del emisor y
  la revisión del perfil. Responde `permit` y `heartbeatTTL` con `granted: false`, porque `granted` es de la
  reserva. El permiso emitido se lee en `permit.state`. Un barrido sin concordancia
  responde `unix-scope-unconfirmed` reintentable, sin permiso.
- `consume_creation {key, permitId, launchDigest}`. `launchDigest` es el digest canónico
  de `launch`. Hace CAS de `Issued` a `Consumed`. `creationAllowed: true` sólo aparece en
  esa respuesta original. Cualquier otra respuesta, incluido su replay, lleva `false` o
  nada, y ausente equivale a falso.
- `record_started {key, permitId, root: {pid, unique}}`. Lee la raíz con `observe_root`:
  hija directa del dueño, en su coalición y su boot, y fuera de la línea base. Pasa a
  `Registered` con el recibo `scp_<digest>`, y la reserva pasa a `running`. Repetir la
  misma raíz con otro `operationId` da `operation-conflict`. Otra raíz deja la reserva
  `uncertain`.

`soltar` efectivo responde según el permiso:

| Permiso | Respuesta |
| --- | --- |
| `Unissued`, `Issued` | Sello `never-created` propio. `released`, permiso `Sealed`, crédito devuelto. |
| `Consumed` | Retiene con `unix-scope-unconfirmed`. Sin raíz no hay barrido que lo cierre. |
| `Registered` | Observa la línea base. `Complete` cierra con evidencia `unix-scope-empty` correlacionada y pasa a `Closed`. `Occupied` retiene con `scopeStatus = {status: occupied, novel (hasta 64 pares [pid, unique]), novelCount}`, reintentable; un miembro nuevo elevado deja además la reserva `uncertain`. `Retry` responde `scopeStatus` `scan-retry`, reintentable. `Invalid` deja la reserva `uncertain`. |
| `Closed`, `Sealed` | Devuelve el cierre original sin crédito nuevo. |

`occupied` y `scan-retry` describen esa observación y nunca quedan en la fila. La
evidencia sintética del fixture ya no libera nada: `soltar` sella con evidencia propia.
Un replay devuelve el resultado original con `granted: false`. `currentState` lleva
`permit` cuando la fila tiene registro de creación, y el replay de un consumo nunca
habilita.

Fencing, en este orden: generación vigente (salvo `soltar`), boot físico de la reserva,
epoch del permiso igual al del proceso, reserva no `uncertain` y revisión del perfil
igual a la del permiso. El epoch del emisor es `next_epoch` de lo persistido, calculado
una vez por proceso. Así cambia tras cada reinicio de hostgate que tenga algo
persistido. Un permiso de otro epoch, un perfil que ya no acredita la revisión del
permiso u otra raíz tras el registro dejan la reserva `uncertain`, que nunca se crea ni
se cierra.

El `Runtime` efectivo conecta `DarwinCoalition` y, sólo si algún perfil trae bloque de
acreditación, corre el selfcheck y lee el build una vez por arranque.
`profile_verified(profile, workload_uid)` es `Accredited` sólo con un selfcheck pasado
cuyo boot coincide con el de `sysctl` (comparados como UUID) y con build y uid
iguales. En sombra no corre nada. `require_creation_provider` sigue lanzando: no hay
bootstrap, ledger ni migración, y Fleet no emite ni consume permisos.

`validate_result(result, mode, store_id, operation)` rechaza los campos de otra
operación y valida la variante pública del permiso contra el estado, `scopeStatus`,
`heartbeatTTL` y `creationAllowed`. El cliente lo aplica a cada respuesta. Con operación, una
respuesta de creación no admite campos de consulta, y su éxito exige el permiso de esa operación
y su campo propio. `state` y `permit` sólo van con `reservationKey`, `running` y `released`
llevan permiso, también en `currentState`, y `creationAllowed: true` exige la reserva
`reserved`. `scopeStatus` `occupied` lleva exactamente `min(64, novelCount)` pares.
