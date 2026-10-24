# Hostgate en sombra

Implementación local Python 3.9 stdlib de `fleet/host-reservation@1`, autoridad
`a1f00fafa2fb7b6146a9575923c6f97074403e10`. Mini ya aceptó la semántica.
Fleet revisará wire/IPC/PR; PAO solicita el OK concreto de Felix para activar.
Este paquete no instala, conecta, ejecuta cargas ni añade daemon o polling.
`nativos=[]`. No modifica el runner, los slots ni los consumidores.

Toda respuesta pública lleva `result: shadow`, `wouldGrant`, `operationId`,
`configRevision`, `exitCode` y `reasons: [{code,detail,retryable}]`. No hay permiso
ni `granted` efectivo. `simulationKey` es exclusivamente diagnóstico de la BD
`shadow.db`: nunca promoverla, copiar sus generaciones a un ledger efectivo ni
usar sus reservas para ejecutar. El estado interno `granted` de una preparación
sólo significa que terminó la simulación de su commit.

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
recursos canónicos, ACL, rutas y TTL del host. `ShadowStore.initialize` exige directorio
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

SQLite WAL/FULL serializa `BEGIN IMMEDIATE`: vista de estado, presupuesto, exclusivos,
contador por host/job, operación idempotente y auditoría se confirman juntos. La vista
compacta con checksum evita repartir escritores y tablas sin caller. No hay transacción
con recursos externos. Un fallo/ACK perdido de commit devuelve exit 3, `wouldGrant:false`;
se relee **la misma operación** sin compensar ni repetir creación. Replay verifica peer/canal y autoriza
el contexto antes de consultar operaciones, muestra `originalResult` y `currentState`,
y siempre `wouldGrant:false`. Un rechazo histórico no se vuelve admisible al mejorar
salud; un cambio de payload da `operation-conflict`. Fencing usa la terna completa.

`estado` abre sólo lectura, separa `state` y `effectiveState`, y muestra presupuesto
por estado/dimensión, recursos, ancla, identidad y edad del latido. No escribe, renueva,
libera o reabre. La próxima mutación persiste expiraciones/dudas antes de decidir.
Preparaciones con EOF, muerte, plazo o identidad desconocida terminan con lápida; no
reviven por una operación nueva del mismo huérfano. Reserva retiene desde commit,
incluso si el dueño muere inmediatamente después de revalidación. Boot/reloj/latido/
control dudosos conservan toda la demanda y exclusivos. Heartbeat fresco no reconcilia.
Pausa, root exit, salud sana, desaparición de slots/BD o restart WSL tampoco liberan.
Cambiar boot drena el físico; reapertura/reconciliación positiva queda pendiente.

`ProcessTable` consulta sólo un PID por vez: libproc/sysctlbyname mediante ctypes en
Darwin, `/proc` con ticks exactos y boot antes/después en Linux. EPERM/lectura parcial
es `unverifiable`; PID reutilizado/zombie es ausencia/diferencia del solicitante, nunca
prueba de cierre de ámbito. No enumera ni mata. ABI/clock en Mini aún requieren una
aceptación específica de plataforma: el test de identidad puntual es evidencia acotada.

Las interfaces `authorize_creation` y `record_started` lanzan `NotImplementedError`.
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
no son evidencia nativa. `soltar` incompleto retiene todo; no hay liberación parcial.

`AuthContext` sólo entra como objeto de la frontera de confianza en proceso, nunca
como JSON. El servicio futuro deberá verificar peer kernel conectado por el supervisor,
o canal heredado más identidad del hijo acreditada; un socketpair del creador no
atribuye automáticamente sus credenciales al hijo. ACL/principal autorizan roles:
TTY, nombres, IDs, nonces visibles o texto de OK no dan autoridad. El CLI no implementa
ese transporte ni finge autenticación con PPID. Misma cuenta OS con escritura del runtime/
BD puede alterar el estado; no se promete aislamiento hostil. Separar cuenta es decisión
de provisión, no requisito oculto para probar sombra.

`salud/lector_desk.py` se trasladó sin cambios. Sus 112 casos/aserciones originales
se ejecutan con parametrización stdlib en `tests/support/cases.py`. `lector_mini`
implementa el contrato U4; recibe boot independiente por parámetro. Usa la ventana
[-60,180] s de B, propuesta del consumidor que U4 no especifica y debe aceptar Fleet.
La identidad del host y ncpu se contrastan con configuración. Sin SEN-3853 el desk no
es sano ni degradado; DefaultShell pendiente/fallido implica cero llamadas al transporte.
No se implementa transporte. Ninguna consulta dispara sondas ni intenta obtener testigo.

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
un helper real de prueba sale por EOF del padre y otro sobrevive con autoexit acotado.
La concurrencia SQLite es real entre clientes del API; IPC/supervisor/launcher/profiles/
adaptador real y aceptación U1 quedan futuros. No se valida activación con estos fixtures.
