# Fleet local — SEN-3820 harness r8

CLI opt-in de un slot FIFO por autoridad local, Python 3.9/3.14 por inspección,
biblioteca estándar, Linux/macOS POSIX. Esta entrega contiene código y pruebas
**escritas, cero ejecutadas**. MacBook sólo permite edición e inspección.

El contrato público es `fleet-job-api@1`; el ledger usa esquema **4**, incompatible
con 2/3, sin migración. El código no activa hostgate, reservas compartidas, Mini,
desk, SSH, Windows, cancelación ni selección de nodos. Los contratos vigentes
permanecen intactos. [Contrato y procedencia](SEN-3820.md) y
[matriz completa de pruebas pendientes](tests/COVERAGE.md).
[Corrección r4 de literales públicos y regresiones contra d5](SEN-3820-r4.md).
[Respuesta r3 y regresiones contra127](SEN-3820-r3.md).
[Antecedente r2 y regresiones contra0b](SEN-3820-r2.md).

## Interfaz

Ejemplos documentales para un futuro entorno autorizado; **NO EJECUTAR aquí**.
El estado debe ser propio, privado 0700, físico, exclusivo y sobre almacenamiento
local con locks/durabilidad adecuados. No usar otro state-dir para eludir retención.

```text
fleet/bin/fleet --state-dir /estado/propio submit --client qa --cwd /proyecto/propio --idempotency-key suite-3820 --artifact reports/result.json -- tool check
fleet/bin/fleet --state-dir /estado/propio status --idempotency-key suite-3820
fleet/bin/fleet --state-dir /estado/propio wait --idempotency-key suite-3820 --timeout 30
fleet/bin/fleet --state-dir /estado/propio evidence j_<32hex> --attempt a_<32hex>
fleet/bin/fleet --state-dir /estado/propio recover
```

`--state-dir` puede ir antes o después del subcomando. Precedencia: argumento,
`FLEET_STATE_DIR`, `${XDG_STATE_HOME:-$HOME/.local/state}/fleet-pilot`. Se rechazan
symlinks en el recorrido físico y estados públicos/ajenos o rutas reservadas
`pao`, `mini-carga`, `u3` bajo raíces de estado. No se cambian sus permisos.
`--client` > `FLEET_CLIENT` > usuario@host es etiqueta; no otorga autoridad.

`submit` acepta argv literal, stdin cerrado, cwd y entorno completos del emisor.
Captura los archivos admitidos antes de vincular la clave y ejecuta una copia
materializada; no ejecuta directamente el origen. `--input`, `--private`,
`--artifact`, `--credential-env` son repetibles. PWD presente se transforma al cwd
materializado. Las dependencias externas y los ejecutables no quedan congelados.
El comando debe ser compatible: no daemonizar, cambiar sesión o escapar del grupo
original. No existe aislamiento de seguridad ni prueba global de procesos escapados.

La clave opcional es ASCII `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`, ámbito
`(authority UUID, uid OS, key)`. Misma clave y contenido efectivo privado igual
reproduce IDs/admisión originales; distinto devuelve conflicto5/existing original.
La comparación incluye manifiesto, bytes reales, HEAD, cwd relativo, argv, entorno,
client, selecciones y declaraciones/políticas. No usa sólo hashes o redacción.
PWD derivable es la excepción; si se declara credencial, también cuenta su valor raw.
Punto no elimina otras selecciones: `--input . --input foo` difiere de `--input .`
aunque hoy capture los mismos bytes. Se validan todas las selecciones explícitas;
sólo se ordenan/eliminan duplicados idénticos y se equiparan automático/punto solo.
Esa equivalencia incluye un origen noGit vacío o un inventario admitido vacío;
punto no exime de validar cada selección adicional.
Un replay no despacha, renueva, repara, libera ni reabre. Sin clave, cada envío es nuevo.

`submit0` sólo confirma admisión histórica. Se imprime después de intentar el
despacho opcional, sin prometer readiness: `wait` posterior puede dar3 transitorio.
Incluso un lanzamiento de supervisor fallido conserva la admisión y produce su
expediente original never-created. `--enqueue-only` omite la solicitud inmediata;
un supervisor ya activo puede reclamar ese FIFO si el control está abierto.

Status/wait/evidence exigen exactamente JOB o `--idempotency-key`; `--attempt`
opcional debe coincidir. No necesitan origen ni entorno del envío. La admisión es
inmutable y va separada de `observation`. Not-found sólo describe una revisión de
esa autoridad. Captura candidata rechazada dice `candidate.accepted=false` y
`key_binding=not_evaluated`; no afirma que una clave histórica esté libre.

Cada respuesta operativa es un JSON de una línea, con `api`, `operation`, autoridad
cuando acreditada y errores `{code,effects}`. No publica argv/entorno/comparador.
`--help` es ayuda humana. Salida perdida o muerte preentrypoint puede impedir JSON.

| Código | Significado |
| --- | --- |
| 0 | Submit: admisión; status/evidence: lectura; wait: complete+succeeded; recover/operador: decisión confirmada, no éxito global de jobs. |
| 1 | Sólo wait: complete+failed (exit positivo o never-created íntegro). |
| 2 | Uso, candidato, selector, estado ausente/ilegible/incompatible, autenticación o prueba inválida. |
| 3 | Wait: incertidumbre/incomplete inmediato; recover parcial/retención; operador insuficiente/stale/bloqueado o persistencia incierta; submit no decidible. |
| 4 | Sólo wait pending: deadline agotado. |
| 5 | Conflicto de contenido/operation-id o generación/época equivocada. |
| 130 | SIGINT manejado del cliente; consultar lo durable, no inferir cancelación ni rollback. |

Los lectores nunca escriben el estado original. Copian BD/WAL bajo lock a un temporal
privado independiente de TMPDIR/TEMP/TMP y consultan la copia. Recover sí es un
escritor explícito: reconcilia publicación/release ordinarios acreditados y reclama
sólo el pendiente FIFO seguro. No adopta procesos, repone permisos, cancela, reintenta
ni continúa operaciones privilegiadas. Ver la tabla total en SEN-3820.md.
Error de apertura/lock/consulta/selector en status/evidence/wait/operator.status da
2/read_only, sin observation inventada. Wait3 sigue reservado a incertidumbre o
completion incompleta observadas;4 al deadline pending;130 a SIGINT manejado.
Un escritor con timeout de lock conserva3/unknown.

## Retención y operación local

Colección incompleta conserva el slot por defecto y puede inmovilizar toda la
autoridad indefinidamente. Finished, exit0, grupo vacío, TTL y salud no autorizan
release. Hace falta cierre original positivo, permiso cerrado y publicación/colección
íntegras para liberación ordinaria. Una señal conserva `signal`, outcome incomplete
(y wait3), aunque con expediente íntegro sí puede liberar capacidad.

Existe salida explícita **sólo pilot_slot**, para capacidad con cierre original ya
acreditado aunque falte evidencia del job. Invocaciones documentales:

```text
fleet/bin/fleet --state-dir /estado/propio operator drain --operation-id drain-1 --reason "investigación local"
fleet/bin/fleet --state-dir /estado/propio operator assess j_<32hex> --attempt a_<32hex> --reservation r_<32hex> --generation 1 --drain-id d_<32hex> --operation-id assess-1 --reason "verificar cierre original"
fleet/bin/fleet --state-dir /estado/propio operator release j_<32hex> --attempt a_<32hex> --reservation r_<32hex> --generation 1 --drain-id d_<32hex> --proof p_<32hex> --operation-id release-1 --reason "resolver capacidad retenida"
fleet/bin/fleet --state-dir /estado/propio operator status --operation-id release-1
fleet/bin/fleet --state-dir /estado/propio operator reopen --drain-id d_<32hex> --operation-id reopen-1 --reason "capacidad reconciliada"
```

Actor nativo OS/owner del estado; no se acepta `--actor`, `--uid`, force ni JSON de
cierre. Motivo UTF-8 de1–512 caracteres sin controles ni secretos declarados.
Drain impide nuevos claims, también el relevo ordinario del supervisor, sin cancelar
carga existente. Assess conserva una prueba ligada al cierre original, época y
revisión relevante. Release revalida y confirma capacidad+decisión+auditoría en la
misma transacción; mantiene drained, colección e historia incompletas.
Reopen requiere reconciliación positiva y época vigente; no despacha por sí mismo.
Después se solicita recover. `already_drained` da0, `not_drained` da3 y época vieja5.

Decisión, binding y auditoría locales son atómicos: status muestra not_found o
committed; **no hay intención pending durable**. Rollback no deja binding; ACK perdido
se resuelve por status/repetición exacta. Otra petición con igual operation-id da5.
Recover devuelve `operator_pending=[]`; nunca completa una acción del operador.
Un conflicto conserva en auditoría `request` con los diez campos canónicos de la
petición denegada, incluidos action/backend/selectores/drain/proof/nulls, saneados
con credenciales persistidas. No cambia el binding/receipt original. Backend
operatorio tiene cota de forma1–128 caracteres sin controles/surrogates; no añade
soporte a ningún backend.

Toda proyección pública operatoria comparte una regla recursiva por procedencia y
esquema. Los literales del protocolo (api, operación, binding/status, acción/decisión,
códigos/efectos, motivos tipados y disposiciones) conservan significado aunque sus
bytes coincidan con credenciales válidas. Los datos copiables se redactan, incluidos
selectores/details/target anidados, motivo, identificadores del solicitante, backend
de request y datos descriptivos del actor. Un backend o motivo copiado no queda
exento por coincidir con un literal público. Campos no reconocidos se tratan como datos.
Recibos y eventos pasan por la regla antes de persistir; respuesta, replay y status
exacto/paginado vuelven a aplicarla. Los lectores usan declaraciones/valores
persistidos de su snapshot, sin confiar en que historia guardada sea segura ni
reescribirla. Se conservan estructura/tipos y atribución. El request crudo permanece
privado para decidir y comparar binding: dos secretos distintos no se vuelven
iguales porque compartan redacción pública. No hay migración ni reconstrucción de
literales que un productor anterior ya hubiera sustituido por marcadores.

`operator status --after-seq N --after-audit-seq A --limit 50` pagina decisiones y
auditoría con cursores independientes. Empezar con N=A=0; después pasar
`next_after_seq` y `next_after_audit_seq` hasta `has_more=false`. Cada lista va en
orden seq y tiene como máximo limit entradas (1–100, default50). Un stream vacío
conserva su cursor; has_more usa ambos. Si se omite el cursor audit, su offset
inicial es N por compatibilidad; para continuar deben enviarse ambos cursores.
El fin describe esa revisión; cada página tiene su copia, sin snapshot concurrente
de todo el recorrido. Pueden conservarse ambos offsets para otra consulta posterior.
Con `--operation-id` se devuelve únicamente el recibo exacto y el control actual.
Ambos cursores admiten enteros entre0 y9223372036854775807 inclusive. Fuera de ese
rango se devuelve2/invalid_pagination/read_only antes de abrir SQLite, también con
selección exacta por ID; nunca se intenta enlazar ese entero. Un máximo válido
sin filas posteriores conserva el cursor y deja vacío su stream, sin agotar el otro.

`--backend host-reservation@1 --profile <id>` admite un trabajo host sólo con una
autoridad hostgate efectiva (`admin.local_authority()`); sin ella responde
2/backend_unavailable sin crear estado, y un replay conserva su recibo. El ledger v5
añade `host_rounds`; un lector deja un v4 intacto y un escritor lo migra con el slot
libre. Un trabajo host admitido queda `queued`: todavía no hay adaptador que pida la
reserva, y el claim del piloto no lo toma ni lo salta.
`status` lee las rondas con forma estricta por estado, JSON canónico y correlación
con el recibo efectivo. Una fila incoherente da `host_correlation_invalid`. Una
ronda `held`, o `outcome_unknown`/`confirmed` sin supervisor verificado, se observa
`uncertain`, nunca completa.
Sin rondas conserva la observación de cola. Este runtime no escribe rondas:
transiciones, adaptador, supervisor y recuperación host siguen pendientes.
Evidencia: `tests/test_host_reservation_cli.py` y `tests/test_host_rounds_cli.py`.
**C21 positivo (ejecutar un trabajo host) permanece abierto; O10 positivo está fuera
de SEN-3820.**

## Persistencia, privacidad y límites

BD WAL/FULL, lock de directorio y transacciones serializan claves/claim/control/audit.
Un job, un intento, un permiso durable no regenerable. Se retienen claves, cápsulas,
historia, pruebas y auditoría sin TTL/purga. Preparaciones fallidas o huérfanas quedan
atribuidas y retenidas; sólo su creador puede retirar una preparación resuelta y no
referenciada. Recover/operador no limpian preparaciones.

Cápsulas, comparador, argv/entorno y auditoría son plaintext privados 0700/0600,
sin defensa frente a root o adversario del mismo uid. Los payloads normalizados
pueden ser0644/0755 dentro de directorios privados. Privados por defecto/ampliables
se excluyen de captura/huella. Secretos conocidos/declarados se rechazan en entradas
admitidas y se redactan en streams; secretos no declarados en archivos ordinarios
pueden viajar. No existe detector universal ni garantía sobre transformaciones.

Límites:20.000 archivos,32MiB/archivo,256MiB de entrada,1MiB de metadata comparable
y1MiB de argv/entorno,256MiB/stream; copia lectora de BD/WAL limitada a256MiB por
archivo. Exceso/fallo no purga asociaciones ni concede permisos. La certificación de
filesystem, semántica nativa de Linux/Darwin y durabilidad siguen pendientes runtime.

## Verificación en Mini — MacBook sólo edita

Usar una copia propia con `remote-run start --sha <HEAD completo> --name
fleet-sen3820-rebase -- <comando>` y recogerla con `remote-run wait <id>`.
Antes de lanzar, comprobar carga/swap. No crear volúmenes ni reparar permisos para
satisfacer precondiciones. Las suites y cualquier setup de cuota son seriales.

Fijar `PYTHONDONTWRITEBYTECODE=1` y `PYTHONPYCACHEPREFIX` dentro de la copia de la
corrida. El harness integrado conserva las rutas físicas y privadas requeridas
por SEN-3821: `FLEET_TEST_RUN_DIR` identifica `remote-run/runs/<id>` y
`FLEET_TEST_FIXTURE_BASE` está bajo esa corrida. `FLEET_TEST_RUN_BASE` apunta a
esa misma base de fixtures para SEN-3820. `FLEET_TEST_CS_BASE` y la configuración
de cuota sólo pueden apuntar a recursos existentes que cumplan las precondiciones.

Repetir con Apple Python 3.9.6 y Python 3.14, sin instalar intérpretes:

```sh
/usr/bin/python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
/opt/homebrew/bin/python3.14 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
/usr/bin/python3 -B -m unittest discover -s fleet/tests -p 'test_ledger_lifecycle.py' -v
/opt/homebrew/bin/python3.14 -B -m unittest discover -s fleet/tests -p 'test_ledger_lifecycle.py' -v
python3 -m unittest discover -s fleet/hostgate/tests
```

Después del rebase, `test_cli.py` reúne 148 métodos: conserva los 64 de r8 y los
100 públicos/de infraestructura de SEN-3821, con 16 compartidos adaptados al CLI
vigente. El caso de lanzamiento fallido cambia de nombre para acreditar admisión
histórica, como exige SEN-3820. Lifecycle añade un método separado. Conservar los
tests no declara que todos sean compatibles con el contrato nuevo ni que pasen.

El inventario r8 siguiente y sus resultados de autoría son históricos.
Inventario textual: se conservan los62 métodos de d5, incluidos los53 r1, cinco r2
y cuatro r3;16 base +28 core +20 operator =**64 métodos**, dos nuevos r4.
R4 añade2 invocaciones subTest (declaración anterior/posterior a la historia), más
un método efectivo sin subTest. Se conservan los29 subTest r3 y27 r2; cero ejecutados.
34 filas contractuales (26 definidas,6 parciales/condicionales,2 positivas abiertas)
y3 requisitos adicionales. Método y subcaso son unidades diferentes.
La matriz detalla precondiciones faltantes; su ausencia falla explícitamente, sin
skip contado como cobertura. El harness usa CLI real, recursos propios, barreras
con watchdog30s y contadores externos; retiene estados incompletos o no atribuibles.
La fixture externa de contención de directorio r2 tiene watchdog60s y gate propio.
Guarda snapshots, campañas, contadores y copia verificada de evidencia pública antes
de retirar fixtures completas en `evidence-<fixture>/public-snapshots.json`.
No hay imports Fleet, SQL, hooks privados ni respuestas simuladas en los tests.

## Harness r8 y evidencia histórica

R8 modifica sólo C07 en core3820_cases.py, esta documentación y la
matriz; `fleet/fleet/**` y `fleet/bin/**` siguen idénticos a c63. Conserva los64
métodos, sin nuevos métodos; O06 declara dos subTest de fases condicionales.
**Cero ejecución de r7/r8**: ni Python/imports/discovery/sintaxis/tests ni gates en
MacBook. El bloqueo inicial del autor r6 fue aceptado; PAO-CLARIFICATION-1 autoriza
los arreglos y el setup r6 sin certificar las fronteras de cuota. R7 corrige sólo
retención temprana, fotografía C08 y respuesta lectora C07 sobre ese antecedente.
R7 conserva A Astra APTO individual y B Opus NO APTO por W1: importtime puede
preceder a la identificación de wait. Esa ventana es posible por código; su
frecuencia en Mini no está medida. R8 no modifica retrospectivamente esos votos.

C07/C08 lanzan exclusivamente sus propios clientes mediante un shim de un hilo
que comprueba SIGHUP/SIGINT=SIG_DFL y registra un marcador antes de exec del
intérprete auténtico. No usa preexec_fn ni cambia señales del padre/transporte.
C07 mantiene el comando running en su barrera y limita la campaña a **3 intentos
totales y10s monotónicos totales**, desde antes del primer lanzamiento hasta la
recogida y comprobaciones del último intento. Cada communicate del lector tiene
timeout≤5s y≤remanente global; importtime y la consulta pública también consumen
ese mismo presupuesto. No se renueva por intento ni se introduce demora50ms,
sleep adaptativo/ciego u otra ayuda de planificación. Watchdog30s y cleanup/cierre
originales siguen intactos. El acuse importtime acredita sólo importación.

Cada intento conserva antes de los asserts número/PID, UTC de inicio, petición y
envío de SIGINT, recogida final observada, rc, stdout/stderr íntegros (incluido el
importtime ya consumido y bytes hex), completitud/errores/faltantes, duración y
remanente, consulta pública y contador. UTC de fin es la recogida observada, no
el instante interno de exit. Datos no observados quedan null o incompletos; no
se vuelca el entorno. external_evidence entrega los registros al snapshot de
cleanup existente; quedan con persistencia no confirmada en memoria, sin prometer
evidencia durable si falla ese registro/cierre. Una observación fallida es explícita
y no autoriza reintento. La imagen de cada intento se compara con la referencia
original y la anterior: estado/vista/revisión/transiciones/reserva/resolución,
control/dispatch/supervisor/hijo/comando y contador exactamente1.

Sólo se repite un cliente **ya terminado y recogido** cuya respuesta JSON válida
sea130/client_interrupted, operation=unknown y error.effects=unknown oread_only,
con invariantes satisfechas e intentos/tiempo restantes. No hay lectores vivos
superpuestos. Timeout, recogida incierta, marcador ausente, JSON inválido, otro
retorno/error/efecto o imagen/contador cambiados fallan sin reintento;0 nunca pasa.
Sólo130/client_interrupted con operation=wait y error.effects=read_only, recogido
dentro del presupuesto y con invariantes, acredita la muestra: main identificó
la operación lectora, **no entrada exacta en el bucle wait**. Unknown nunca es PASS
ni cobertura. Agotar3 intentos/10s sin muestra falla **BLOCKED de preparación del
harness**, sin skip verde ni atribuir defecto al producto. El registro deja total
de intentos, unknown_count y sus UTC de señal disponibles, con faltantes explícitos,
para una futura estimación PAO de la ventana real. Ninguna frecuencia se ha medido.
Tras la muestra se conserva apertura del gate, terminal/wait y cierre originales.

C08 espera marcador y running público durable
con self.ready("c08", ready) antes de fotografiar admitted; así no compara starting
con una transición running legítima posterior. Conserva sesión propia, SIGHUP real
al grupo, salida por esa señal en5s, supervisor original,
reserva/historia y contador1; settle sigue10s. Setsid/restore_signals no se toman
como normalización. Sólo la barrera de lost_supervisor omite su print decorativo;
done ya no depende del lector de stdout que el caso mata con SIGKILL. Los demás
usuarios de BARRIER conservan sus logs y oráculos.

Los dos r4_a01 conservan imágenes físicas completas, incluidos SHM/WAL y
modo/tamaño/mtime/ctime/hash, antes y después de ventanas de lectores puros.
El replay escritor se comprueba por CLI antes/después: autoridad/uid/revisión,
operaciones y auditoría completas con IDs/seq/recibos/tiempos propios, ambos
cursores, control y todas las observaciones de los holders salvo observed_at.
No añade historia ni cambia capacidad/dispatch; siguen los contadores sin entrada,
los literales, el binding raw, privacidad y los asserts posteriores de r5.
Un cambio físico de SHM no se presenta como reescritura lógica.

C20/O06 siguen **CONDICIONALES/BLOCKED**, nunca skip verde. Configuración futura
explícita: `FLEET_TEST_QUOTA_BASE=/Volumes/pao-cs/fleet-tests`,
`FLEET_TEST_ISOLATED_QUOTA=1`, `FLEET_TEST_QUOTA_FILL_LIMIT` positivo ≤536870912.
El setup exige disco disk3s7, UUID F4FF2E7E-751F-48B3-BA0D-F5F2656C23BC,
Case-sensitive APFS, Owners Enabled, cuota exacta512MiB y base física cosmonaut0700.
Revalida con diskutil en el futuro entorno autorizado; un env no es certificado.
No monta, redimensiona ni cambia permisos. Fallos de certificado, falta de espacio
previo o cambios de metadatos ajenos bloquean; nunca se limpia para ganar espacio.

Cada intento activa retención administrativa **antes del primer quota_record,
preflight o mkdtemp**. Cleanup conserva el directorio original y su journal incluso
sin raíz de cuota ni admisión y aunque la primera escritura falle; el snapshot
exterior no sustituye esos originales. Sólo una creación mkdtemp devuelta se registra
como raíz creada; la atribución exige dev/ino/uid/modo verificados antes de recorrerla.
Si alcanza el setup, prepara state/project/home/fixtures dentro de esa raíz0700.
Inventaría metadatos inmediatos del base sin descender a historias ajenas y sólo
su raíz atribuida recursivamente. Registra fase administrativa real separada del
objetivo, fallo primario, errores secundarios y cada inventario como complete,
incomplete con entradas parciales/error, o not_performed con motivo. Finally intenta
las observaciones permitidas y el registro final también ante fallo temprano.
Consultas de certificado conservan rc/stderr o error/timeout; no publica el entorno
privado. La misma excepción primaria se relanza después de observación/persistencia
y cierre de descriptores; sin primaria previa se propaga el primer fallo real.
El journal se intenta escribir y sincronizar fuera de pao-cs. Sólo una escritura,
fsync de archivo/directorio y cierres completos dejan persistence=synced en memoria;
un fallo conserva unconfirmed y sus errores, sin prometer durabilidad. Se retienen
los originales disponibles aunque tampoco pueda persistirse el diagnóstico final.
La estabilidad de
metadatos ajenos **no acredita ausencia de archivos abiertos o de uso ajeno**.

Actualmente el caso se detiene explícitamente después del setup: **no admite
carga, crea gates ni llena cuota**. No implementa una observación causal nueva.
Rc3/persistence_unconfirmed/operation_not_found puede proceder de apertura previa
del ledger y no demuestra fallo en commit de capacidad. C20 exige además ack
terminal propio; un gate/contador fallido no demuestra colección terminal. Por
ello el setup no puede producir PASS. O06 intenta registrar por separado los
prerrequisitos de release y reopen mediante subTest, sin fingir assess/release.

Una futura decisión PAO sobre un discriminante admisible debe preceder a cualquier
implementación de agotamiento. Sólo ENOSPC/EDQUOT real y frontera objetivo probada
permitirían PASS: C20 colección/publicación terminal, O06 release tras assess y
reopen tras release. Deben conservarse error2/3, wait/recover3, contador1 y consulta
not_found o committed con auditoría/capacidad/control correlacionados. Barreras
preparadas antes de llenar, inventarios y guardas contra uso ajeno son obligatorios.
Sólo el relleno propio atribuido podría retirarse en finally, registrándolo; estados,
raíces, evidencia e históricos permanecen. En r7 no existe relleno ni retirada.

C23 permanece BLOCKED por CLI auténticos2/3 e historial propio>7d, sin reloj/SQL ni
skip. A6, C21 positivo, O10 host-side, ventanas internas y negativos OS no se cierran.
La suite histórica c63 run20261026-143952-e9b9eb dio3.9=56pass/6fail/2error y
3.14=58pass/4fail/2error; C20/O06/C23 fueron3 BLOCKED por stack. C08 tuvo63/64
reportes públicos por cierre no acreditado entonces. Su cierre posterior por
ausencia a20:50:26Zrc0 no rellena reportes ni cambia el resultado. Las hipótesis de
señales/BrokenPipe históricas siguen delimitadas; no se midió getsignal de esos
clientes. Los tres discriminantes contra3b son recorridos estáticos: antes el fallo
previo a quota_roots podía borrar el journal; C08 podía fotografiar starting; C07
aceptaba la respuesta exterior unknown/unknown. No se midió FAIL/PASS de esos
recorridos ni se declara aceptación runtime de r7. Lost_supervisor y replay r6
permanecen intactos. El discriminante r8 contra af es estático: unknown completo
recogido deja de abortar por identificación temprana y sólo habilita otro intento
acotado; agotamiento no acredita wait. No hay FAIL/PASS r8 medidos. N1 rótulos r6
no motiva barrido; N2 KeyboardInterrupt secundario durante finally de cuota sigue
declarado, sin tocar operator ni precedencia de la primaria. Replace/tamaño/refactor
y los demás nits históricos permanecen diferidos.


---

## Histórico de SEN-3821

La interfaz y el esquema 3 de los apartados siguientes describen SEN-3821
antes de SEN-3820. La interfaz vigente y el esquema 4 están arriba. Se conservan
las garantías de captura y el historial de verificación revisados en main.

# Fleet local pilot — SEN-3821

CLI opt-in, Python **3.9+**, biblioteca estándar, macOS/Linux POSIX. Un slot y
cola FIFO por state-dir propio, sin daemon, perfil, Git ni manifiesto manual
obligatorios. No integra ni activa PAO, Mini, desktop o hostgate.

```sh
export FLEET_STATE_DIR=$(mktemp -d)
fleet/bin/fleet submit --cwd /ruta/proyecto -- python3 check.py
fleet/bin/fleet status j_...
fleet/bin/fleet wait j_... --timeout 30
fleet/bin/fleet evidence j_...
```

`submit` copia y verifica las entradas antes de registrar. La aceptación no espera
que termine el comando. El recibo devuelve `job`, `attempt`, `registered`,
`accepted`, `input` e `input_policy`. `input.commit_sha` identifica HEAD o es
`null` sin Git; `input.effective_sha256` identifica rutas, bytes, tamaños y modos
admitidos. Son identidades diferentes. `cwd_rel` identifica el cwd dentro de la
captura. Las consultas conservan esa identidad aunque cambie o se borre el origen.

Se ejecuta argv literal, sin shell implícito y con stdin cerrado. El entorno
**íntegro recibido del emisor** permanece privado, incluido PATH y las variables
volátiles, también en cola. `fleet-context@2` persiste junto a esa representación
la transformación `PWD=materialized-cwd` y la tabla `fleet-search-paths@1`.
Al ejecutar, PWD se deriva del cwd materializado; su ruta aleatoria no entra en
la representación comparable. No se reescriben argumentos ni otras variables.
Un contrato de contexto desconocido se rechaza antes de consumir la compuerta
o crear el comando. El recibo sólo publica la política, sin valores del entorno.

Referencias literales detectables a la raíz del origen en argv o archivos
admitidos se rechazan. Además del texto literal de la raíz, se comprueban paths
absolutos de argv, literales entre comillas simples/dobles (incluidos espacios)
y tokens absolutos delimitados por espacio, NUL, comillas o `;|<>(){}[],=` en
argv/contenido. Se conserva y comprueba el literal completo, además de sus
posibles componentes separados por `:`, también en posición no inicial. Un
nombre POSIX individual puede contener `:`; ese carácter no acredita una lista.
En argv también se conserva el sufijo absoluto hasta el final del elemento
recibido, incluidos espacios tras prefijos como `--config=`, `-I` o `VAR=`.
No se tokeniza por shell ni se modifica argv.
Primero se busca el texto de la raíz; después se comprueban cantidad, longitud
y profundidad de todos los candidatos antes de resolver ninguno físicamente.
Se admiten hasta 40.000 candidatos por valor, 16.384 bytes por ruta y 128 barras
por ruta, incluida la ruta completa de un argumento absoluto. Una lista puede
contar de nuevo sus componentes al inspeccionar también sus tokens. Los ancestros
se recorren de forma perezosa, sin materializar una tupla de prefijos. El límite
de 16 KiB conserva la fixture binaria anterior de 8 KiB; es una cota de trabajo,
no una afirmación de PATH_MAX del host. Una ruta/candidato que excede una cota
rechaza el envío con `source_reference_limit`, incluso dentro de un binario;
no se considera segura por no poder resolverla. Esto puede rechazar notebooks,
CSS o JSON con base64 textual reconocido como candidato excesivo, aunque el
archivo tenga menos de 32 MiB. Los 32 MiB por archivo son un máximo adicional,
no una promesa de admitir cualquier archivo menor que ese tamaño. La comprobación
de contención compara dispositivo/inode y ancestros existentes, también bajo
otra grafía de mayúsculas cuando el filesystem identifica el mismo objeto.
No convierte rutas a lower/casefold ni equipara objetos distintos en un volumen
sensible a mayúsculas. Las variables de búsqueda/carga validadas son exactamente:

```text
PATH                            :
PYTHONPATH                      :
LD_LIBRARY_PATH                 : o ;
LD_PRELOAD                      : o espacios
DYLD_LIBRARY_PATH               :
DYLD_FALLBACK_LIBRARY_PATH      :
DYLD_FRAMEWORK_PATH             :
DYLD_FALLBACK_FRAMEWORK_PATH    :
DYLD_INSERT_LIBRARIES           :
NODE_PATH                       :
RUBYLIB                         :
PERL5LIB                        :
```

Son separadores literales, sin expansión de shell, `~` o tokens del cargador.
Para admisión, un componente vacío de una lista de paths se resuelve como cwd;
los tokens vacíos de LD_PRELOAD se ignoran. Los relativos, incluidos `..`, sólo
se admiten si tanto su normalización léxica como su resolución de symlinks desde
cwd quedan dentro de la raíz capturada. Se conservan literalmente y después se
resuelven desde el cwd materializado. Esto permite herramientas secundarias por
PATH relativo, no sólo el ejecutable principal. Los componentes absolutos que
solapan el origen, sus ancestros o aliases resolubles se rechazan, aunque argv[0]
sea externo. En directorios absolutos externos también se comprueban los aliases
symlink hijos inmediatos hacia el origen o sus ancestros. Se mantiene el orden
de búsqueda externo A/B del job. Un nombre de biblioteca sin `/` en LD_PRELOAD
conserva la resolución propia del cargador del host.

La validación admite hasta 1.024 componentes y 65.536 caracteres por variable,
y examina hasta 40.000 entradas inmediatas de directorios externos distintos.
Los rechazos usan `source_reference_in_environment`, `search_path_escapes_capture`,
`search_path_limit` o `search_alias_limit`, sin reflejar nombres/rutas/valores.
No recorre recursivamente dependencias externas, no expande código o tokens
dinámicos ni inspecciona variables fuera de la lista. Las herramientas externas
y sus dependencias siguen disponibles y pueden cambiar: esta política acotada
no acredita un sandbox ni congela el runtime, aliases externos futuros o código
construido dinámicamente.

EACCES, EPERM o ELOOP al obtener una identidad necesaria producen
`path_identity_unavailable`, sin publicar rutas. La excepción conserva ruta y
errno para el diagnóstico interno. Un symlink hijo inmediato ajeno cuya
identidad no puede observarse no invalida por sí solo un directorio de búsqueda
accesible: se registra el error y se continúa. Los aliases observables hacia el
origen o sus ancestros siguen rechazados. Para argv[0] sin `/` se contrasta la
primera herramienta regular ejecutable observable en orden PATH; un candidato
inaccesible anterior permite continuar hacia una herramienta válida posterior.
Si no se encuentra una y hubo identidad inaccesible/cíclica, se rechaza. Un
comando inexistente conserva su resultado de spawn fallido. Los argumentos
absolutos, referencias de contenido, componentes de búsqueda, privados, HOME,
cwd y estado conservan sus comprobaciones físicas.

Los errores de esos hijos especulativos (incluidos ENOENT, ENOTDIR y
ENAMETOOLONG) se guardan al capturar en
`<state>/input-diagnostics/<job>.json`: variable, enlace, ruta que falló y errno
reales, hasta 64 registros y 65.536 bytes de registros JSON, más su envoltura y
cuenta de omitidos. El directorio es privado 0700 y el archivo 0600. Puede
contener rutas y valores de credenciales declaradas: tiene la misma sensibilidad
que el entorno privado del ledger. No entra en cápsulas, huella, bundles,
recibos públicos ni logs; no se publica su contenido sin redacción. No es una
exploración de dependencias ni acredita que una carga futura no invoque un enlace.
También se comprueba la identidad de entradas recorridas antes de descartarlas
por Git: un symlink cíclico ignorado puede rechazar el envío. Es el contrato
conservador de esta edición; no cambia el orden del inventario ni abre aliases.
ENOENT, ENOTDIR y ENAMETOOLONG del filesystem significan que ese objeto no tiene
identidad observable; aún se comparan sus ancestros y el texto literal. Otros
errores se propagan al manejo normal del CLI. Las cotas se comprueban antes de
realpath/stat/ancestros y de nuevo sobre el resultado de realpath.

Opciones de `submit`, repetibles salvo `--cwd` y `--client`:

- `--cwd DIR`: cwd de origen; por defecto el actual.
- `--input PATH`: estrecha la selección a archivos/directorios relativos a la
  raíz de captura. En Git la raíz es el toplevel; sin Git es cwd. `.` admite la
  selección automática completa. No permite sobrepasar exclusiones.
- `--private PATH`: excluye esa ruta y descendientes relativos a la raíz;
  reconoce archivos/directorios existentes por dispositivo/inode y ancestros,
  incluidas otras grafías que designen el mismo objeto. Objetos distintos en un
  volumen sensible a mayúsculas siguen siendo distintos. Los defaults conservan
  los patrones literales indicados abajo.
- `--credential-env NAME`: declara como secreto el valor no vacío de esa
  variable, conservando su entrega al comando mediante el entorno privado.
- `--artifact RELPATH`: salida obligatoria relativa al cwd materializado.
- `--client LABEL`: etiqueta declarativa sin autoridad. Precede a FLEET_CLIENT
  y al valor por defecto `user@host`.

```sh
fleet/bin/fleet submit --cwd /ruta/repo --input src --input tests \
  --private src/local-account.data --credential-env SERVICE_ACCESS \
  --artifact reports/junit.xml -- python3 tests/check.py
```

La selección automática `fleet-inputs@1` considera archivos rastreados y untracked
no ignorados en Git; captura los bytes efectivos del **working tree**, nunca
selecciona el índice como contenido ejecutado. Fuera de Git considera archivos
regulares bajo cwd. HEAD y catálogo del índice se inspeccionan y revalidan con
Git local, entorno controlado, sin hooks, filtros, fsmonitor, descargas ni
operaciones de checkout. Un repositorio con HEAD ilegible o aún sin commit se
rechaza; el modo sin Git no requiere preparar un repositorio. No se copian
metadatos VCS ni estado Fleet.

Las rutas privadas por defecto son, literalmente:

```text
.ssh  .aws  .azure  .config/gcloud  .gnupg  .kube
.env  .env.*  *.pem  *.key  .npmrc  .pypirc  .netrc
credentials  credentials.json  .fleet  .local/state/fleet-pilot
```

Los patrones de un componente se aplican a cada componente de ruta; las rutas
con `/` son prefijos desde la raíz. El state-dir seleccionado y metadatos
`.git`, `.hg`, `.svn` también se excluyen. Un privado o ignored explícitamente
solicitado no se admite. El recibo muestra la lista por defecto, ampliaciones
`--private`, categorías excluidas y alcance de redacción.

Los valores de variables cuyos nombres terminan en `_TOKEN`, `_SECRET`,
`_PASSWORD`, `_API_KEY` o `_PRIVATE_KEY`, además de AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, GH_TOKEN, GITHUB_TOKEN y OPENAI_API_KEY, son credenciales
conocidas. `--credential-env` amplía esa declaración. Se rechazan esos valores
en nombres o bytes de entradas admitidas y artefactos publicables; se redactan
en stdout/stderr recogidos, incluso partidos entre bloques. No se publican
argv, entorno, valores ni hashes de credenciales. El entorno completo y argv
permanecen en el ledger privado, en claro, sin cifrado.

`submitted_by.user` procede de la cuenta del uid, sin confiar en LOGNAME/USER.
Sus textos de usuario/host se redactan con los secretos del emisor antes de
persistirse. El lector usa la declaración privada guardada con el job para
sanear usuario/host, `client`, `detail`, textos descriptivos de la política y
`transitions.actor`, incluidos actores heredados de otro emisor durante un relevo.
Sólo `supervisor.host` es descriptivo: PID, boot/inicio, IDs de job/intento,
secuencias, timestamps, estados, versiones y códigos conservan su valor.
La identidad original privada se conserva para comprobar dueño. Cambiar el
entorno del lector no cambia esa redacción ni concede autoridad a `client` o
a los metadatos publicados.

**No se inspecciona el contenido de archivos arbitrarios para descubrir secretos
no declarados.** Un secreto desconocido en un archivo normal sí viaja en la
instantánea y puede aparecer en la salida. El consumidor debe declarar sus
rutas privadas y credenciales. La redacción cubre valores literales conocidos o
declarados; no sus transformaciones, codificaciones ni efectos externos del
comando. La prueba negativa de un secreto ficticio no declarado documenta esa
cota aceptada por PAO. Esta aceptación de producto no aprueba el código ni el PR.

Cada componente se abre sin seguir symlinks. Se rechazan especiales, submódulos,
escapes y colisiones Unicode/casefold. Se copian bytes a archivos nuevos, sin
compartir hardlinks, ACL, xattrs u otros metadatos del origen. Contrato de modos
`exec-bit;mtime=0@1`: archivos 0755 si tenían cualquier bit ejecutable, 0644 en
otro caso; mtime cero. El bit ejecutable participa de la huella. Directorios
privados 0700; no se acredita la identidad de directorios vacíos independientes
del cwd. La copia verifica stat antes/después, segundo inventario y hash; un
cambio observado produce `source_changed`, sin reintento automático.

Límites por captura: 20.000 archivos, 40.000 entradas de inventario, profundidad
128, rutas de 2.048 caracteres, 32 MiB por archivo y 256 MiB totales. Hasta 256
valores secretos, 64 KiB de valores únicos. Antes de inventariar se rechazan HOME
declarado, HOME del uid y sus ancestros, además de un origen igual o contenido
en el state-dir. Se comprueban cwd y raíz Git. Un state-dir dentro de un proyecto
permitido se excluye de la captura. HOME, sus ancestros, origen y estado se
comparan también por identidad física de objetos y ancestros existentes; una
variante de mayúsculas que el volumen resuelva al mismo objeto conserva las
guardas y exclusiones. Se conservan las aperturas por componentes
sin symlinks. Los límites fallan explícitamente, sin truncar entradas admitidas.

El layout separa `capsules/<job>/{manifest.json,payload}`, la copia mutable
`attempts/<attempt>/workspace` y `attempts/<attempt>/bundle`. La cápsula se
sincroniza antes de referenciarla en el commit de registro. Se materializa en
archivos nuevos y se verifica contra la huella antes del único spawn autorizado.
Cambiar o borrar el origen ya no altera lo ejecutado; el comando puede modificar
su propio workspace. La cápsula no congela intérprete, herramientas, dependencias
ni runtime, y no aísla frente a un proceso con el mismo uid.

Las salidas declaradas deben estar ausentes en el origen y antes de ejecutar,
sin solaparse con entradas u otras salidas. No se borran reportes preexistentes.
El productor escribe sus paths habituales; no necesita llamar a un publicador.
Tras terminar la raíz y quedar vacío su grupo propio, el supervisor copia
artefactos regulares estables, abiertos por fd, a un bundle exclusivo.

Sólo el supervisor crea los streams por intento, O_EXCL y sin symlinks. Consume
stdout/stderr concurrentemente mediante pipes, con contrapresión y memoria
acotada. Observa EOF, lectura, escritura, fsync y cierre. Un límite de 256 MiB por
stream, error o bytes perdidos invalidan la colección aunque el comando salga 0.
No se inventan logs para un comando `never_created`.

Un manifiesto final `fleet-evidence@1` compromete job, attempt, entrada, resultado
del comando, errores y tamaños/hashes/identidad de los archivos recogidos. Su raíz
se persiste en la misma transacción que resultado, liberación y claim FIFO
siguiente. `status`, `wait` y `evidence` verifican esa raíz y los payloads. Ausencia,
truncación, reemplazo, contenido incorrecto o manifiesto ajeno degradan la vista,
sin cambiar el resultado persistido ni liberar capacidad. `evidence JOB` devuelve
rutas privadas, tamaños/hashes y validez, nunca contenidos.

`succeeded` requiere exit 0 y colección válida; exit positivo con colección
válida o creación inequívocamente fallida produce `failed`. Señal o colección
inválida produce `incomplete`. `command` conserva ese resultado separado de la
colección; `collection` conserva errores incluso si no pudo sellarse el bundle,
y `recorded_outcome` permite observar una pérdida posterior. Dos reportes
de bytes iguales de intentos distintos pueden ser válidos. El sello acredita
transferencia y atribución, no frescura semántica de un reporte viejo que el
productor haya copiado deliberadamente a una salida nueva.

`--state-dir` antes o después del subcomando precede a FLEET_STATE_DIR y a
`${XDG_STATE_HOME:-$HOME/.local/state}/fleet-pilot`. Estado local propio, privado
al uid, sin symlinks ni archivos ajenos. No se reparan permisos. Se preserva el
rechazo de `pao`, `mini-carga` y `u3` bajo las raíces de estado. No usar un
filesystem remoto o sin locks SQLite y flock de directorio comprobable.

Esquema **3**, sin migración, upgrade ni compatibilidad implícita con esquema 2.
El bootstrap se serializa antes de comprobar directorio vacío/BD; se prueba
exclusión de flock en runtime y se falla si no puede acreditarse. WAL/FULL se
configuran antes de escribir estado durable. Cada transacción y cierre del
ledger comparte ese lock. Para evitar modificaciones de SHM por SQLite, los
lectores copian BD/WAL bajo lock a un temporal privado acotado (256 MiB por
archivo) y consultan esa copia; no reparan ni crean archivos en el state-dir.
Toda escritura, transacción y cierre futuro sobre la BD original debe pasar por
`Ledger` y ese flock, incluido un cierre que pueda hacer checkpoint. Su detección
de inicio de transacción requiere SQL que comience literalmente por `BEGIN`;
un cursor o API SQLite que eluda Ledger invalidaría el protocolo de copia.
No sustituirlo por `mode=ro`, `query_only` o `immutable=1` sobre el original.

Las operaciones ordinarias conservan el timeout de flock de diez segundos.
El cierre terminal de una conexión original usa adquisición bloqueante sin ese
timeout: el dueño conserva la conexión y el lock hasta completar cierre SQLite,
incluido checkpoint, y limpieza propia. Un lector/escritor cooperante detenido
puede prolongar indefinidamente la salida del cliente o supervisor que cierra;
agotar la espera ordinaria no autoriza abandonar la conexión al destructor nativo.
Una excepción nativa de cierre conserva el descriptor/lock para responsabilidad
explícita del dueño, sin afirmar recuperación automática. No se acreditan cierre
ante señales que interrumpan el proceso, SIGTERM/KILL, muerte del intérprete,
fallos de flock/cierre del sistema ni durabilidad ante pérdida de energía.
El descriptor que usará el escritor se adquiere antes de abrir SQLite; si falla
esa apertura de descriptor no existe aún una conexión original que pueda escapar
al destructor. Si el bootstrap SQLite falla, cierra bajo el flock exterior y
se retira el descriptor aún no transferido.

La base temporal del lector es exclusivamente `/tmp`, resuelta físicamente antes
de llamar a `tempfile`, sin consultar `gettempdir()` ni modificar el entorno.
Se ignoran TMPDIR, TEMP y TMP aunque señalen al estado, a un descendiente o a
un alias. En Darwin habitual `/tmp` resuelve a `/private/tmp`; en Linux habitual
permanece `/tmp`. La base y cada ancestro físico se abren por componentes sin
symlinks: deben pertenecer a root y no permitir escritura a grupo/otros, salvo
la base con sticky bit. Se comparan dispositivo/inode del estado con la base y
sus ancestros para rechazar una base dentro del estado, también mediante aliases
de directorio. Sólo después se crea un directorio privado exclusivo con `dir`
explícito. SQLite y la limpieza operan sobre esa copia propia.

Si `/tmp` no existe, no puede resolverse/validarse, está dentro del estado o no
permite crear la copia, la consulta falla cerrada con exit 2; no busca otra base
ni prueba escritura en el estado. Esta elección requiere una jerarquía temporal
administrada por root en Darwin/Linux; layouts distintos pueden perder
disponibilidad. No garantiza aislamiento frente a un atacante arbitrario del
mismo uid, root o cambios privilegiados del namespace/montajes durante la
consulta. No se ha medido aquí espacio temporal, latencia ni RSS.

SIGTERM/KILL durante una consulta puede dejar `/tmp/fleet-read-*` (habitualmente
`/private/tmp/fleet-read-*` en Darwin), con copia plaintext de BD/WAL, argv y
entorno privados, fuera del state-dir. Los directorios son 0700 y los archivos
copiados 0600; no aíslan de otro proceso del mismo uid. No hay limpieza global ni
handler nuevo de señales. La salida normal limpia su copia; una terminación
abrupta puede dejarla hasta la limpieza del sistema o retirada atribuida por el
dueño. No buscar ni borrar temporales de otras consultas/corridas.

Cada consulta copia y sincroniza BD/WAL bajo exclusión antes de abrir SQLite en
el temporal; no copia SHM del original. Este coste y la espera de escritores
se infieren del código, sin benchmark. `read_stable` puede mantener bytearray y
bytes simultáneamente; sellado/verificación también cargan logs completos hasta
256 MiB. La cota de 256 MiB por archivo de ledger puede impedir leer un estado
acumulado grande: es un límite de disponibilidad, sin cifra medida de RSS o
latencia. El soporte de flock de directorio, WAL, checkpoints, timestamps y fsync
de archivos/directorios debe acreditarse en Mini.

La ejecución privada versionada fija commit/null, huella y modos, cwd relativo,
argv literal, entorno completo, contrato de salidas y versión de contexto. No
añade claves, idempotencia ni recuperación de SEN-3820. Cada submit crea otro job:
**no reenviar automáticamente** ante pérdida de respuesta.

Claim liga attempt/generation. El supervisor se vincula condicionalmente una
sola vez y compromete su identidad antes de R. Consume una compuerta durable
antes de Popen; un permiso perdido no se regenera. READY confirma aceptación;
NOT_CREATED positivo permite relevo; UNCONFIRMED conserva slot. Un error tras
posible spawn, supervisor perdido o identidad host/boot/PID/inicio no verificable
retiene capacidad. No hay adopción ni reconciliación host-side.

Para un envío sin claim, el recibo lleva `observation=durable_admission`:
`state=queued` y `view=queued|uncertain` describen la admisión dentro de la misma
transacción, con la observación del dueño del slot efectuada antes del commit.
No se abre un lector después de confirmar el registro para fabricar el recibo.
Un dueño ausente/no verificable o un grupo drenándose conserva `view=uncertain`
y exit 3 con `job`, `attempt`, `registered=true` y `accepted=true`. La observación
de admisión puede ser anterior a la respuesta: el job ya podría haber avanzado.
`status`/`wait` consultan su estado actual; el recibo no mezcla queued con una
vista terminal, no reconstruye permiso y no autoriza reenviar el trabajo.

Después de un registro confirmado, un error manejable al completar lanzamiento,
NOT_CREATED/relevo o cierre conserva `job`, `attempt`, `registered=true`, entrada
y política. Devuelve exit 3, `view=uncertain`,
`error=post_registration_unconfirmed` y `observation=durable_admission`;
`state=starting|queued` describe la admisión, sin inventar un resultado terminal.
`accepted` conserva lo confirmado hasta ese punto (queued o READY). No abre otra
vista, reintenta, libera ni reconstruye permisos para completar ese recibo. Un
relevo parcialmente confirmado puede haber avanzado otros trabajos; consultar
las identidades sin reenviar. Antes de confirmar registro siguen siendo posibles
errores sin IDs. Se mantiene el límite declarado de interrupción por señales
durante cierre; esta corrección no amplía el manejo de Ctrl-C.

Si se acredita que no se creó supervisor y se completa NOT_CREATED/relevo,
submit devuelve exit 1, IDs, `registered=true` y `accepted=false`, sin `outcome`.
Ese recibo acredita no creación; `status`/`wait` son la fuente del resultado de
evidencia. Si el sellado falla, el comando sigue siendo `never_created`, pero
el resultado es `incomplete` y wait devuelve 3. No se añade una lectura del
ledger para completar el recibo ni se deduce `failed` del exit 1 de submit.

El supervisor y comando tienen sesiones separadas del cliente. SIGINT/SIGHUP
del cliente no cancelan la carga; el supervisor ignora ambas señales. El comando
debe ser compatible con el piloto: no daemonizar ni sacar descendientes del
grupo. Un descendiente dentro del grupo retiene slot e informa incertidumbre
hasta su cierre. Escapes de sesión/grupo quedan fuera de la garantía del piloto;
no usar esas cargas ni activarlo como autoridad de hosts. No editar el runtime
con supervisores activos. Sin prioridad, cancelación, nodos, SSH ni activación
host-side; Git Bash/Windows no soportado.

Cada respuesta operativa es un JSON en una línea. Diagnósticos saneados, sin
excepciones que reflejen comandos o valores. `--help` es ayuda humana. `status`
puede informar `view=uncertain` con exit 0. Ningún lector inicia, libera o reencola.

| Exit | Contrato |
| --- | --- |
| 0 | submit sin duda en su observación de admisión/lanzamiento; status/evidence lectura; wait éxito acreditado |
| 1 | submit no creó supervisor, accepted=false; wait failed acreditado |
| 2 | Uso/entrada/estado inválido, esquema incompatible o job desconocido |
| 3 | submit lanzamiento, dueño o finalización posterior al registro inciertos; wait incomplete/uncertain |
| 4 | wait agotó timeout de lectura; la ejecución continúa |
| 130 | SIGINT del lector, incluso durante importación; JSON de interrupción |

Un job queued puede estar aceptado y devolver 3 si su dueño era incierto al admitirlo.
`registered=true` acredita registro durable; `accepted` no acredita éxito.

## Verificación pendiente exclusivamente en Mini

MacBook sólo edición/inspección. El coordinador encarga runtime del head exacto
en Mini por `run.sh` y FIFO original autorizado, sin modificar FIFO129, que sólo
caracteriza su head anterior. Ejecuta en una copia propia del candidato:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_ledger_lifecycle.py' -v
```

La base cde tenía 80 métodos escritos, sin ejecución de ese candidato. Se
conservan 73 cuerpos de método sin cambios; cuatro métodos refuerzan oráculos o
preparación (loader, dos lectores y ciclo de vida), y se sustituyen las tres
fixtures grandes de coste por fronteras pequeñas de longitud, profundidad/base64
y cantidad. Se añaden siete regresiones: cuatro de literal completo con `:`,
NOT_CREATED/sellado incompleto,
fallo previo al entrypoint sin JSON y ciclo ignorado con estado inicializado.
Ése era el inventario de review4: 86 métodos públicos y uno focalizado, 87.
Fase B conserva los 87 nombres: 84 cuerpos sin cambios, tres oráculos o
preparaciones modificados (ciclo ignorado, NOT_CREATED y cierre), y nueve
regresiones nuevas de PI/F1: fase B tenía 96 escritos (95 públicos y uno
focalizado). H2 añadió tres de infraestructura, llegando a 99; h3 preserva esos
99 nombres y añade dos de infraestructura. H4 conserva esos 101 nombres y sólo
cambia cuatro casos WAL y sus helpers, más README/COVERAGE; producto e999 intacto.
El candidato h4 tiene **101
escritos = 95 públicos + 5 de infraestructura + 1 focalizado; cero ejecutados**.
H5 conserva esos 101 métodos y sólo cambia la preparación del incompatible y
su documentación: **101 escritos, cero ejecutados en h5**. Producto e999 intacto.
H1–h3 cambiaron ubicación, diagnósticos, atribución y garantías de snapshot/error;
conservar métodos no acredita equivalencia runtime. Las tres entradas funcionales son
16.385 bytes, 516 bytes de base64 textual y 160.004 bytes binarios; mantienen
exit 2/`source_reference_limit`, refutación previa a registro y timeout de 15 s.
El estrés de 4/12 MiB queda **PENDIENTE de contención efectiva coordinada**,
fuera de la suite funcional y sin skip verde. No ejecutar un rojo potencialmente
OOM; ni RLIMIT_AS ni RSS advisory acreditan una cota dura Darwin por declaración.

Antes de asignar temporales, ambos harnesses exigen rutas existentes, absolutas,
físicas y propias suministradas por el coordinador al launcher autorizado:

- `FLEET_TEST_RUN_DIR`: directorio real `remote-run/runs/<runId>`, sin escritura
  de grupo/otros; el harness no deduce HOME ni runId.
- `FLEET_TEST_FIXTURE_BASE`: base y jerarquía privadas (0700) físicamente bajo
  esa corrida, en filesystem que permita los aliases case-insensitive exigidos.
- `FLEET_TEST_CS_BASE`: única base externa autorizada, existente, física, del
  uid y exactamente 0700, sin symlinks ni fallback. Cada uno de los dos métodos
  `case_sensitive_distinct` y el de cuenta/host reales crea una raíz exclusiva
  `fleet-cli-<runId>-py<major>-<minor>-pid<pid>-<azar>` allí. Mide case-sensitivity
  con objetos propios y conserva identidades distintas. Las regresiones h2/h3 del
  helper también crean recursos propios allí; el inventario actual está en COVERAGE.

Sin variable válida se falla antes de mkdtemp. No hay fallback a T compartido,
HOME o `/tmp`, ni creación/montaje de volumen. Si las bases disponibles no
pueden satisfacer ubicación y filesystem, queda pendiente la precondición de
plataforma. Para h3 Felix creó el volumen APFS sensible en disk3, cuota 512 MiB,
`/Volumes/pao-cs/fleet-tests` de cosmonaut 0700; PAO comunicó certificado con
Owners Enabled en preferences82. H4 no inspecciona ni modifica el volumen.
La suite no lo busca ni monta.
Las dos precondiciones h1 no hacen skip: falta de base sensible, FS incorrecto o
ruta externa incompatible con los secretos reales fallan explícitamente. Cuenta
y host mantienen sus valores reales y el estado vive en la raíz externa; el
prefijo `/Users/cosmonaut` de la corrida de evidencia no provoca skip. Los otros
skips históricos de aliases no cambian ni acreditan cobertura.
La matriz, selector rojo/verde y fronteras sin fixture están en
[`tests/COVERAGE.md`](tests/COVERAGE.md).
Los casos públicos usan subprocess del CLI, fixtures y recursos propios; no
importan Fleet ni consultan SQL en el original. H4 valida además una copia
aislada del main file. La prueba focalizada importa el Ledger real
en un subprocess propio y usa primitivas externas para retener un lector más
de diez segundos antes de liberar. No reproduce públicamente la finalización
nativa durante copia BD/WAL. Los cuatro casos WAL comparten `closed_database_bytes`
por el MRO existente: status finished con job/attempt exactos, settle,
guard_admissions y EX no bloqueante del directorio físico, propio y privado.
La forma quieta admite exactamente ausencia de WAL/SHM o WAL regular de 0 B
con SHM presente. Main y sidecars presentes deben ser regulares, del uid,
privados y con un solo link; lstat no confunde un symlink colgante con ausencia.
WAL con contenido, un único sidecar, archivos inseguros o cambios observados
fallan. Se compara la imagen recursiva completa, incluido el directorio,
identidad, modo, uid/gid, nlink, tamaño, mtime/ctime y hash.

WAL0 no prueba checkpoint ni integridad. El helper lee por fd el main físico
sin seguir symlinks, contrasta identidad/hash y header SQLite/application_id FLT1/
user_version3. Copia sólo esos bytes en un archivo nuevo 0600 `closed-main.db`
bajo la raíz propia, sin WAL/SHM. Abre SQLite únicamente en esa copia, mode=ro e
immutable=1, y exige bytes idénticos, integrity_check=ok, application_id/user_version
y el mismo job/attempt finished, outcome persistido y transiciones completas
por IDs, estados, secuencias y tiempos. Así acredita que ese main por sí solo
contiene el estado terminal; no atribuye autor ni momento de checkpoint, ni
integridad a SHM por existir. No modifica el original ni borra/mueve/trunca
sidecars, ni cambia journal_mode, fuerza checkpoint o usa SQL de preparación.
Una preparación no acreditada conserva la copia que exista (datos privados),
imagen y closed-database-preparation.json; el cleanup retiene la raíz.
Antes de alterar `user_version` en la copia incompatible se mantiene esa
validación independiente. No cambia el error esperado
`unsupported Fleet pilot schema; no migration` por uno genérico. La comparación
con WAL activo sigue separada e íntegra. Esto no acredita resuelto el rojo 3.9
de FIFO129: esa corrida era de b5ca4987, nunca de cde ni de h4.

La evidencia posterior h3 `893160c4` consta en
`D/reports/SEN-3821-suite-r1-diagnostico.md`, SHA256
`9c1de08ffe45a2f2b8377d1682be0a4a08f07bac56bafe86cce60b9fda7c2b9e`:
3.9.6 obtuvo 97 pass/4 fail/0 error/0 skip; 3.14.7, 101/0/0/0.
`D=/Users/felixddhs/.claude/orchestrate/fleet-pruebas`.
El custodio PAO075b50e8 leyó metadatos a **18:25Z del 2026-10-26**, recogidos en
`D/reports/metadatos-sqlite-075b50e8.md`, SHA256
`052d7024e2cc51d8de80847f90c6cc15e37ba58ecd4763f295b74347908a8f08`:
Xcode3.9 enlaza libsqlite3 de Apple con persist-WAL por defecto; Homebrew3.14,
SQLite upstream. Es un hecho de plataforma atribuido al custodio, no una
medición de esta autoría ni sólo una diferencia de versión SQLite. El
journal_mode de `:memory:` no se extrapola al ledger. El cierre de Fleet no
prescribe unlink de sidecars.

Por lectura de 893, los dos reader_temp fallaban por WAL.exists tras cierre;
sólo se capturó ese booleano, no tamaño ni SHM final. Los dos correction fallaban
en la preparación: sólo allí se midió main40960/WAL0/SHM32768. H4 sustituye esa
equivalencia cierre→unlink por el contrato anterior y conserva los oráculos
posteriores: 72 consultas quietas, 72 activas, done ausente y released, watchdog30/
error91, sondas transitorias, WAL>32B/SHM activos, lecturas terminales y
unsafe/empty/absent/incompatible sin reparación. Las 72 activas medidas en h3
no prueban el caso quieto WAL0; h4 necesita su suite101×2 tras APTO doble nuevo.
No se declaran aquí PASS/FAIL de h4 ni aceptación integral. El detalle por caso
está en COVERAGE. Los apartados históricos describen las garantías y límites
de sus respectivas autorías; la evidencia h3 no equivale a ejecución de h4.

H5 parte de `4083b397794df995abf2b286b93fa87245eb7d3c`, autorizado por Felix vía
PAO075b50e8 ~23:05Z del 2026-10-26. La suite h4 posterior dio100 pass/1 fail en
Apple3.9 y101 pass en3.14. La sonda A (`D/reports/SEN-3821-incompatible-probe-A-resultados.md`,
SHA256 `23729dead80845fbec3a5de5a1b0bb4a2f97be8afdde7e6b02aa006afb098a68`)
localizó la excepción Apple en application_id, antes del schema check: el main
derivado conservaba header WAL2/2 sin sidecars. Connect/foreign_keys pasaban;
3.14 leía999 y creaba sidecars en su copia directa. Los originales CLI quedaron
invariantes. No hay error numérico3.9 disponible; CANTOPEN es atribución del
custodio. El saneamiento genérico de un archivo no abrible no era un bug producto.

La preparación h5 conserva `closed_database_bytes` y los otros tres casos WAL.
Sólo una copia nueva0600 en `incompatible-preparation`0700 se abre rw, tras
acreditar el main original. Comprueba modo wal/delete, checkpoint TRUNCATE
(0,0,0) sin busy/frames pendientes en WAL, o (0,-1,-1) si ya era DELETE, y exige
journal_mode=DELETE con retorno delete antes de cerrar. Lee entonces el main:
header SQLite/FLT1/schema3 y18/19=1/1. Una copia independiente en
`incompatible-validation` se abre con mode=ro sin immutable y debe conservar
integridad, IDs/finished/outcome e historia por seq/IDs/estados/tiempos, así como
su imagen completa. Sólo después deriva999 por60:64 y valida otra copia ro con
esa versión antes de escribir el fixture incompatible. No cambia18/19 a mano,
siembra tablas ni hace SQL/checkpoint sobre originales o WAL activo.

El oráculo sigue siendo rc2 y `unsupported Fleet pilot schema; no migration`,
con status/wait/evidence e imagen íntegra antes/después de unsafe/empty/
incompatible/absent. Una preparación fallida retiene copias, imágenes y causa;
intenta `incompatible-preparation.json`0600 sin sustituir la excepción primaria
si fallan cierre/observación/persistencia. No declara un pase ni informe durable
cuando falla esa persistencia. N5 excluye del paquete todas las copias privadas
nuevas de preparación/validación/incompatible, con cualquier nombre, además de
ledger/WAL/SHM/closed-main y entorno privado. Los seis nits h4 siguen diferidos.
H5 quedó cerrado en el PR 31: reviews Astra high + Opus high con APTO doble,
veto PAO y suite Mini h5 (turno 340) con 101/0/0/0 en ambos stacks.

Las pruebas incluyen
snapshots sin Git, Git/índice/working tree,
privacidad positiva y negativa, entorno/PATH en cola, streams simultáneos y un
fallo de escritura mediante RLIMIT_FSIZE real, artefactos inválidos y pérdida
posterior, concurrencia inicial, lecturas sin efectos y rechazos de entradas.
El límite público ejercita escritura; no afirma inyectar separadamente un fallo
de cierre/fsync. El soporte concreto de flock y durabilidad sigue pendiente.

La tabla loader de Fleet se ejercita con Python ya arrancado: un bootstrap
propio introduce el entorno del job y ejecuta el script real `bin/fleet` mediante
runpy, conservando argv/exit/JSON, sin importar Fleet desde la fixture. Un caso
separado usa PYTHONHOME propio vacío para fallar antes del entrypoint. Un loader
puede actuar en esa frontera nativa distinta; los reportes129 no contienen el
stderr causante y no acreditan que ésa fuera su causa observada.

Cada admisión se inventaría antes de lanzar, incluidos wrappers/directos y
concurrencia. Un JSON ilegible conserva rc/stdout/stderr y produce un diagnóstico
explícito. Sólo se refuta por ausencia con cliente terminado y BD ausente
antes/después en el mismo estado exclusivo, sin borrado intermedio. Timeout o
respuesta perdida conservan incertidumbre si falta esa acreditación. El negativo
del ciclo ignorado usa otro oráculo explícito: BD inicializada, emisores recogidos, EX propio
mantenido durante el CLI, respuesta exacta de identidad no disponible y estado
completo invariable, incluidos BD/WAL/SHM si existen. No exige su desaparición.
NOT_CREATED también parte de status terminal positivo y procesos propios
recogidos, sin exigir ausencia de sidecars. Esa refutación no se aplica a JSON ilegible.
La limpieza exige atribución y ausencia efectiva de procesos/grupos propios
mediante señal 0; ante duda falla y conserva los recursos que realmente quedan.
Las raíces ordinarias se retienen bajo la corrida y las externas en CS_BASE.
Antes de retirar una raíz externa segura, la evidencia se copia a un directorio
0700 `external-fixture-evidence-*` en FLEET_TEST_FIXTURE_BASE bajo runs/<id>:
respuestas CLI originales, diagnósticos, publicaciones y snapshot/mapa con hashes,
en archivos privados 0600 sin seguir symlinks. Archivos, directorio de evidencia
y su padre se sincronizan antes del borrado. La enumeración de cápsulas, attempts
y contenido de bundles usa scandir y propaga sus errores, incluido ENOENT:
no certifica un árbol vacío por fallo de lectura. Sólo la ausencia previa por
lstat de una ruta opcional se registra como tal. Las admisiones atribuidas exigen
cápsula/payload/manifest e intento; el estado CLI auténtico terminado con colección
sin bundle_seal_failed exige manifest del bundle. No se inventan bundles para
casos refutados o sin sellado. Una enumeración/copia incompleta o ruta exigida
omitida impide acreditar snapshot y retirar originales; no se copia ledger ni entorno.
El snapshot `external-fixture.json`
y sus copias son inmutables; `removal-result.json` registra aparte la ausencia
observada. Resultado ausente/parcial o persistencia fallida deja confirmación
incierta. `cleanup-failure.json` conserva causa y estado observado sin reemplazar
el snapshot ni ocultar la excepción original; si falla también, la secundaria se
encadena y se intenta stderr. Aunque fallen write/flush de stderr, el llamador
recibe la misma primaria con causas secundarias disponibles en memoria; no se
declara diagnóstico durable ni cierre confirmado por ello. Un fallo previo impide borrar, rmtree fallido puede dejar
datos parciales y el fallo posterior puede encontrar la raíz ausente. No se
promete retención íntegra tras intentar retirarla ni se reintenta borrar trabajo
incierto. Las regresiones h2 congelan la primera imagen y exigen una sola escritura
de snapshot antes del borrado, conservando bytes/hashes e identidad física.
H2/h3 prevén retener intencionalmente raíces de fallo previo/parcial y de
enumeración, además del control refutado, para recogida atribuida con evidencia
bajo la corrida. Los fallos stderr comprueban raíces ya ausentes y snapshot intacto.
En h4 no se ha ejecutado ni creado ningún recurso de prueba; el inventario de
la corrida h3 está en el diagnóstico citado, sin retirar sus residuos.
En `<base>/fleet-cli-*` quedan
`admission-*.json`, `cli-json-*.json`, `closed-database-preparation.json` cuando
corresponda y el estado/cápsulas/intentos originales. El caso sin JSON conserva
su diagnóstico incluso si la ausencia se refuta. La focalizada conserva
`<base>/fleet-lifecycle-*/lifecycle-client.json` y estado ante fallo de cualquier
oráculo. Retiene SH durante 12 s, exige invariancia BD/WAL y SHM existente antes
de liberar, conexión realmente cerrada, salida 0, recogida y EX posterior.
Registra tamaño/hash de los archivos existentes antes de liberar y tras cerrar;
no exige borrar WAL/SHM ni infiere checkpoint por su presencia o ausencia.
No se mueve ni oculta una carga incierta para limpiar. Barreras con
autoexit, deadlines acotados y ningún proceso ajeno señalado. No hay instalaciones
ni CI nuevo.

El watchdog de 30 segundos de la matriz de 144 consultas se conserva: su autoexit
da error y `done=watchdog`, nunca liberación sana. Su duración y margen siguen
pendientes de medición en Mini. No se han aumentado timeouts para ocultar un rojo.

Las pruebas nuevas se ejecutaron en la suite Mini h5 (turno 340). El candidato
se cerró con esa evidencia, reviews Astra high y Opus high con APTO doble, veto
PAO y autorización de Felix, y se mergeó en el PR 31.

El diagnóstico fase A de f967 ejecutó 87 métodos por intérprete: 3.9.6 obtuvo
18 pass/67 fail/0 error/2 skip y 3.14.7 obtuvo 19/66/0/2. No fue aceptación:
PI impidió alcanzar muchas conductas; los otros seis F2 no fueron seis rojos
WAL medidos y F1 sólo tuvo análisis estático. La prueba de cierre 3.9 llegó a
`closed`/exit0 y falló después por exigir ausencia WAL; no se midió PERSIST_WAL
ni su causa SQLite. La lectura PAO69 acreditó un enlace ajeno con EACCES en un
directorio PATH accesible y otro dangling con ENOENT; no conserva el stat
histórico de las 61 trazas PI ni es un replay de ellas. Los límites de volumen
case-sensitive y de ubicación de metadatos se resuelven estáticamente en h1;
su verificación tuvo la corrida h3 citada. H4 requiere APTO doble nuevo y
verificación propia; el estrés y las fronteras no ejercitadas siguen pendientes.
