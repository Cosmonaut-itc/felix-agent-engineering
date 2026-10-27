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

`--backend host-reservation@1` se rechaza explícitamente en submit y operador.
**C21 positivo permanece abierto; O10 positivo está fuera de SEN-3820.** Estos
rechazos no son integración, simulación ni prueba positiva de hostgate.

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

## Suite futura — NO EJECUTAR en MacBook

PAO prepara una copia nueva del head congelado y el entorno propio, sólo después de
reviews estáticas nuevas, veto PAO y orden posterior, en su turno autorizado Mini.
La implementación h5/r6 puede ser paralela en checkouts disjuntos; sus suites y
cualquier llenado de cuota permanecen serializados, sin concurrir con SEN-3821.
Esta entrega no prepara copia, payload, launcher ni FIFO.

Con `FLEET_TEST_RUN_BASE` físico, propio0700 dentro de la futura corrida, repetir con
cada intérprete real provisto (3.9 y3.14), sin instalar ni sustituir interpretes:

```sh
PYTHONDONTWRITEBYTECODE=1 /ruta/python3.9 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
PYTHONDONTWRITEBYTECODE=1 /ruta/python3.14 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
```

Un único TestCase hereda mixins; discovery sólo `test_cli.py` evita duplicación.
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
