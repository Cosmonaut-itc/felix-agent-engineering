# SEN-3821, harness h4 sobre h3 y producto e999 intacto

Sólo inspección estática. Ninguna prueba se ha ejecutado en este encargo.
La suite pública está en `test_cli.py`, que incorpora cinco mixins y contiene
además cinco regresiones de infraestructura separadas del CLI. Se parte de las
69 conductas escritas de e469add3: 16 heredadas, 21 de contenido, 18 de corrección r1,
dos de temporales r2, once integrales y una focalizada de ciclo de vida.
Sobre cde (80 métodos escritos, cero ejecutados de ese candidato), quedan 73
cuerpos de método sin cambios, cuatro métodos con oráculo/preparación reforzados
(loader, dos lectores y ciclo de vida), tres fixtures de coste sustituidas y
siete métodos nuevos. Ése fue el inventario review4 de 87 métodos.
Fase B conserva sus 87 nombres y 84 cuerpos sin cambios; modifica la preparación
de NOT_CREATED, el oráculo del ciclo ignorado y el cierre focalizado, y añade
nueve métodos. En fase B eran 96 escritos: 95 públicos y uno focalizado.
Los helpers comunes de atribución/ubicación/diagnóstico también
cambian; conservar cuerpos no prueba equivalencia runtime. La dimensión de
estrés retirada no se cuenta como conservada ni ejecutada.
En la suite pública no se importa Fleet ni se consulta SQL sobre el original.
H4 valida además una copia aislada del main file, descrita abajo. Cada submit se
registra antes de lanzar; el rechazo por ausencia exige estado exclusivo,
cliente terminado y BD ausente antes/después, sin borrado intermedio. La
refutación específica del ciclo ignorado con BD existente se describe abajo.
Un fallo de atribución o de cierre propio conserva las fixtures en su base y
la evidencia bajo la corrida.
La ronda h1 preserva los 96 nombres, sin métodos añadidos. Cambia sólo las bases,
la evidencia/limpieza externa y dos cuerpos integrales: precondición sensible sin
skip y cuenta/host reales sin skip por el home. Conserva todos sus oráculos y
añade comprobaciones explícitas de salida y metadatos redactados. Cero ejecutados.
H2 preserva esos 96 métodos y añade tres de infraestructura: 99 escritos.
H3 preserva los 99 nombres y añade EXACTAMENTE dos métodos de infraestructura:
**101 escritos = 95 públicos + 5 de infraestructura + 1 focalizado; CERO
ejecutados en aquella autoría**. Los subcasos no son métodos adicionales. Es inventario textual,
sin discovery ni garantía runtime.
El APTO estático doble de producto e999 se conserva como antecedente. H1 no
obtuvo APTO doble de harness y h2 tampoco; h3 obtuvo APTO estático doble.
H4 preserva los 101 métodos, sin añadir ni reclasificar ninguno, y sólo cambia
los cuatro casos WAL y sus helpers, README y COVERAGE. Cero ejecutados en h4;
requiere reviews nuevas y después suite exacta en ambos intérpretes.
Los apartados históricos describen cobertura escrita y límites de sus autorías;
sus menciones a «sin ejecutar» no sustituyen los resultados h3 citados abajo
ni se convierten en resultados de h4.

## H4: quietud física y validación independiente del main file

Autoridad: decisión Felix transmitida por PAO075b50e8 ~19:05Z del 2026-10-26,
preferences85. Base h3 `893160c43c3ede2f5f98f94fa03cc095194353ac`; producto
`e999e40033d754f790f82d726c458afda2b1281b` intacto. El diagnóstico público
`D/reports/SEN-3821-suite-r1-diagnostico.md` (SHA256
`9c1de08ffe45a2f2b8377d1682be0a4a08f07bac56bafe86cce60b9fda7c2b9e`)
registra h3: Python3.9.6 97 pass/4 fail/0 error/0 skip; Python3.14.7
101/0/0/0. Son mediciones anteriores, no resultados de h4. `D` es
`/Users/felixddhs/.claude/orchestrate/fleet-pruebas`.

Hecho de plataforma atribuido al custodio PAO, lectura **18:25Z**:
`D/reports/metadatos-sqlite-075b50e8.md` (SHA256
`052d7024e2cc51d8de80847f90c6cc15e37ba58ecd4763f295b74347908a8f08`).
Xcode3.9 enlaza la libsqlite3 de Apple, con persist-WAL por defecto; Homebrew3.14
enlaza SQLite upstream. La diferencia no se reduce a una versión semánticamente
más nueva. No se extrapola el journal_mode de `:memory:` al ledger ni se repite
la medición del custodio. El producto configura WAL/FULL, serializa cierre y
transacciones, y copia BD/WAL para leer; no prescribe unlink de los sidecars.

`closed_database_bytes`, compartido por los cuatro casos mediante el MRO
existente, conserva status finished con job/attempt exactos, settle,
guard_admissions y flock EX no bloqueante. Bajo EX comprueba dueño, privacidad
e identidad física del directorio. `quiet_database_image` admite exactamente
dos formas: ni WAL ni SHM, o WAL regular de **0 B con SHM presente**. Main y
sidecars presentes deben ser regulares, propios, privados y tener un solo link.
Usa lstat: un symlink, también colgante, no significa ausencia. WAL con contenido,
WAL sin SHM, SHM sin WAL, tipos/permisos/dueño inseguros o cambios observados
fallan; no se borran, mueven ni truncan sidecars para preparar la fixture.
Se contrasta identidad/modo/uid/gid/nlink/tamaño/mtime/ctime de la observación
con la imagen, y la imagen recursiva completa antes/después debe coincidir.

**WAL0 no prueba checkpoint ni integridad.** El helper lee el main físico por
fd sin seguir symlinks, contrasta identidad/hash y conserva la comprobación
de header SQLite, application_id FLT1 y user_version3. Escribe exclusivamente
sus bytes en `closed-main.db`, copia nueva privada bajo la raíz propia, sin
sidecars copiados. SQLite se abre sólo sobre esa copia con mode=ro e immutable=1:
comprueba bytes copiados, integrity_check=ok, application_id/user_version y el
job/attempt finished con outcome persistido y secuencia completa de transiciones
(IDs, estados, secuencias y tiempos) iguales al status original. Eso acredita
que el main por sí solo contiene ese estado terminal; no prueba quién hizo
checkpoint ni cuándo, ni valida SHM por su mera presencia. No usa SQL de
preparación ni cambia journal_mode, fuerza checkpoint o abre SQLite en el original.
Ante fallo conserva la copia que exista, imagen y diagnóstico
closed-database-preparation.json; el cleanup existente retiene la raíz.
La copia contiene datos privados del ledger y queda 0600; no se publica.

Discriminante por lectura de 893: `reader_temp_environment_preserves_quiet_state`
fallaba en la preparación por WAL.exists; ahora acredita el cierre y la imagen
principal antes de sus 72 consultas hostiles. `reader_temp_environment_preserves_live_wal`
fallaba al exigir unlink final, después de sus 72 consultas activas y released;
ahora valida la forma quieta y el main tras ese mismo cierre. En ambos el paquete
sólo capturó WAL.exists=True al final: **no** tamaños ni SHM final.
`readers_reject_unsafe_incompatible_and_empty_state_without_repair` y
`reads_preserve_active_wal_shm_and_checkpointed_state` fallaban en el helper:
sólo esos dos diagnósticos midieron main40960/WAL0/SHM32768. El primero conserva
la copia incompatible auténtica por header999 y el error exacto
`unsupported Fleet pilot schema; no migration`, más unsafe/empty/absent invariables.
El segundo conserva carga activa, WAL>32B/SHM, running/wait4/evidence e imagen,
y después los tres lectores succeeded y transiciones intactas sobre el estado
terminal comprobado independientemente. Ningún oráculo posterior se retira.
Las 72 lecturas activas no acreditan por sí mismas el caso quieto WAL0: las
72 quietas y la suite101×2 del head h4 siguen pendientes. No se añade skip ni
se declara aceptación integral; watchdog30/error91, barrera FIFO, done ausente,
released y sondas transitorias observables conservan su fuerza.

## Cinco regresiones de infraestructura h2/h3, sin ejecutar

`ExternalFixtureEvidenceTests` usa el helper real sobre raíces físicas propias
en CS_BASE y evidencia privada bajo la corrida. No importa ni ejecuta Fleet,
no simula case-sensitivity, identidad, cuenta ni host. Inyecta fallos sólo en la
escritura del helper, enumeración de sus directorios o su retirada, y en stderr
local. Sus datos binarios y metadatos de admisión describen recursos propios,
no respuestas CLI fabricadas. Son cinco métodos escritos por nombre, sin
contarlos como conductas públicas ni haber ejecutado discovery:

- `test_h2_post_removal_persistence_preserves_snapshot_and_original_error`:
  tras snapshot sincronizado, elimina realmente la raíz y escribe sólo un
  prefijo inválido del resultado separado antes de lanzar OSError controlado.
  Exige ausencia por lstat, snapshot/mapa/copias byte a byte inalterados y hash
  esperado, la misma excepción y confirmación incierta. Un segundo subcaso
  hace fallar también el diagnóstico: conserva la excepción primaria y encadena
  la secundaria, con ambas causas y observación en stderr. El error de cleanup
  es esperado por la regresión; no se convierte en cierre confirmado.
- `test_h2_snapshot_failure_prevents_removal_and_preserves_original_error`:
  falla tras escribir un prefijo inválido del snapshot, antes de acreditarlo.
  Exige que no se intente retirada, raíz/identidad/datos originales intactos,
  copia disponible, snapshot inválido, causa original y ningún resultado de cierre.
- `test_h2_partial_removal_preserves_snapshot_and_original_error`:
  con snapshot válido, retira realmente un archivo propio y falla antes de
  retirar el hermano y la raíz. Exige archivo ausente, hermano intacto, identidad
  de raíz conservada, mapa/hashes/copias intactos, causa auténtica y ninguna
  confirmación de ausencia. El helper informa presencia con contenido incierto,
  sin prometer retención íntegra tras rmtree parcial.
- `test_h3_enumeration_errors_prevent_removal_and_preserve_originals`:
  en cápsulas, attempts y contenido de bundle, inyecta EIO y ENOENT de scandir
  sobre directorios físicos propios con admisión atribuida y estado terminado.
  Exige la misma excepción/errno, snapshot no acreditado, ninguna retirada y
  bytes/identidades originales intactos. Tres omisiones de publicación exigida
  (manifest de cápsula, intento atribuido y manifest de bundle) deben fallar
  también. Un control de admisión refutada permite estado ausente y exige su
  registro como ausencia opcional, con recibo/hash conservado.
- `test_h3_stderr_failure_preserves_primary_error_and_snapshot`:
  tras retirada real y fallo de persistir resultado y diagnóstico, falla write
  o flush de stderr. Exige la misma excepción primaria, causa de persistencia
  encadenada y referencias a ambas secundarias, ausencia física observada,
  confirmación incierta y snapshot/mapa/hashes/objetos físicos intactos.

Los escenarios h2 congelan sólo la primera imagen persistida y exigen exactamente
una escritura del snapshot antes de retirar; una reescritura posterior no mueve
el valor esperado. La comparación incluye bytes e identidad de los objetos.

Cada invocación crea otra raíz exclusiva y acredita el probe sensible real.
Los subcasos son seriales; no trasladan la suite pública al volumen.
La evidencia se conserva incluso si falla una aserción. Las raíces del fallo
previo y del fallo parcial quedan retenidas intencionalmente con los datos que
realmente quedan. También quedan las nueve raíces de fallo de enumeración h3
y el control refutado; los dos subcasos stderr observan raíces ya retiradas.
Éste es el diseño de retención para una futura corrida, no recursos creados en
este encargo. No hay cleanup automático que reintente borrar tras el error.
Se imprimen sus rutas y evidencia para la recogida atribuida por el coordinador.
Estas regresiones no miden EIO nativo, probabilidad de fallo, pérdida de energía
ni durabilidad efectiva en Mini. Los fallos de persistencia productivos siguen
con sus límites previos; h2/h3 comprueban exclusivamente este protocolo del harness.

## Nueve regresiones nuevas para review5, sin ejecutar

- F1: rechazo de alias externo propio `fixed:alias with space/run.py` hacia el
  origen, acreditado por samefile/dev/inode sin texto canónico de la raíz. Se
  prueban `--config=`, `-I` y `VAR=` en el elemento argv intacto. Un segundo
  método entrega esas formas externas válidas con espacios al comando y exige
  igualdad literal. Otro coloca un enlace ELOOP antes de sufijos excesivos en
  el mismo elemento: exige `source_reference_limit`, no resolución del enlace.
  Incluye longitud en bytes multibyte, 16.384 bytes y 128 barras, sin ampliar
  cotas ni retirar los componentes `:` no iniciales.
- PI: dos métodos seleccionan una herramienta externa propia en un directorio
  accesible junto a un enlace ajeno. El target tiene EACCES real bajo chmod0 o
  ENOENT real tipo cursor; stat de enlace y target deben acreditar el errno.
  Exigen ejecución correcta y diagnóstico privado con enlace/ruta/errno reales.
  El nombre contiene una credencial ficticia declarada: las respuestas,
  cápsulas, bundles y log supervisor deben excluirla; el diagnóstico privado
  0700/0600 sí la conserva fuera de las publicaciones y de la huella.
- PI: un método exige rechazo ante el ejecutable PATH inaccesible o cíclico,
  otro rechaza su alias seleccionado hacia el origen mutable (bare y absoluto).
  Dos métodos conservan el orden PATH con candidato anterior ENOENT o EACCES y
  herramienta posterior válida; comprueban resultado y diagnóstico. Todos usan
  recursos propios, sin mocks/root ni rutas del sistema modificadas. Una
  precondición física incumplida falla: no es skip ni cobertura acreditada.

El diagnóstico de hijos especulativos está acotado a 64 registros/65.536 bytes
de registros más envoltura/cuenta de omitidos. Errores inesperados y errores de
identidad necesaria conservan rechazo; no se desactiva la comprobación física.
Los errores necesarios conservan ruta/errno en la excepción interna y sólo el
código seguro en JSON público. No se añade un escáner dinámico de dependencias.

Fase A de f967 fue diagnóstico: 87 métodos por intérprete, 3.9.6 con
18 pass/67 fail/0 error/2 skip y 3.14.7 con 19/66/0/2. F1 sólo estático; los
otros seis F2 se bloquearon por PI antes de sus oráculos. PAO69 midió una causa
concreta suficiente (enlace ajeno EACCES bajo PATH accesible), no el stat de las
61 trazas históricas ni un replay. Case-sensitive no estaba disponible: un
método falló y otro hizo skip; metadatos cuenta/host también skip. El padre debe
resolver las precondiciones antes de la verificación autorizada. Ni esos skips
ni métodos PI/no alcanzados cuentan como funcionalidad verde de fase B.

## Siete regresiones públicas nuevas para review4

- Cuatro métodos `r4_colon_`: argv y contenido con symlink externo fijo cuyo
  nombre contiene `:` (y espacio), y argv/contenido con raíz `Project:Source`
  bajo otra grafía acreditada físicamente. Antes de enviar, todos exigen
  samefile y dev/inode del archivo real; el texto canónico de la raíz no aparece
  en el literal. Se exige rechazo específico antes de registro. Se conservan
  ambos casos de componente no inicial y los positivos de objetos distintos.
  El literal completo y sus componentes se validan antes de resolver ninguno;
  `:` no prueba que el valor sea una lista. No se añade expansión ni AST.
- `r4_not_created_receipt`: FIFO propio antes de spawn, cápsula sólo como
  candidata y status positivo registered/starting/supervisor=null con IDs antes
  de obstruir el destino bundle con un archivo propio. Tras liberar el FIFO y
  recoger el cliente, el recibo auténtico atribuye los mismos IDs, exit1,
  registered=true, accepted=false y ningún outcome. status y wait acreditan
  never_created, collection incomplete, bundle_seal_failed y outcome/recorded_outcome
  incomplete; wait debe dar exit3. No SQL ni respuestas simuladas. finally
  libera/recoge y sólo restaura el log tras acreditar cierre propio.
- `r4_pre_entrypoint_failure`: PYTHONHOME propio vacío impide arrancar el CLI
  real. Debe terminar sin JSON/BD, con stderr de encodings. `call` falla con
  diagnóstico rc/stdout/stderr persistido, y sólo la ausencia acreditada en
  estado exclusivo refuta el submit. No se trata como rechazo correcto Fleet.
  Se conserva intacto el negativo de admisión aceptada pero no atribuida;
  guard_admissions no se relaja.
- `r4_ignored_cyclic_symlink`: Git inicializado y estado propio previamente
  ejecutado/cerrado. Git debe confirmar que ignora el symlink y stat acreditar
  ELOOP sin seguir recursos ajenos. Se mantiene EX propio durante el nuevo CLI;
  se exige exit2/path_identity_unavailable y la imagen completa invariable,
  con BD existente, incluidos WAL/SHM si existen. La refutación requiere además identidad del
  descriptor del estado y una segunda adquisición EX rechazada. Si no se
  acredita, queda pendiente y no se borra la carga. Este oráculo sólo vale para
  esa respuesta JSON exacta; no se usa para refutar stdout ilegible.

En fase B, NOT_CREATED y ciclo ignorado exigen status positivo finished con IDs
propios y outcome succeeded para la semilla, recogida y guard_admissions. No
usan la preparación del main file de los lectores. El ciclo conserva EX durante
todo el CLI y compara la imagen íntegra (inventario, identidad, modos, dueños,
tamaños, mtime/ctime y hashes de todos los archivos, incluidos BD/WAL/SHM).
NOT_CREATED conserva sus barreras, obstrucción y recibo auténtico sin outcome,
así como status/wait incompletos y wait3. Aquella preparación de fase B no
alteró correction_cases ni reader_temp_cases; h4 los cambia sólo según su
contrato WAL anterior, sin tocar NOT_CREATED ni ciclo ignorado.

La tabla loader se prueba por `bin/fleet` real usando un bootstrap propio que,
después de arrancar Python, introduce las variables del job y ejecuta el script
con runpy, argv y exit originales. No llama funciones de producción ni importa
Fleet desde la fixture. El fallo previo al entrypoint es otra frontera. FIFO129
no publicó el stderr causante: no se le atribuye una causa nativa observada.
Ante JSON ilegible se conservan rc/stdout/stderr, incluso con exit no cero;
sin cliente terminado y BD ausente antes/después en estado exclusivo no hay
refutación. Cada admisión conserva un recibo privado de atribución y la
exclusividad se revoca si otro envío pendiente comparte estado.

## Delta consolidado r2, pendiente de ejecución

1. Privados: `r2_private_case_aliases` exige aliases existentes por dev/inode
   y samefile de un archivo y un directorio. Los declara con otra grafía,
   comprueba ausencia de ambos y de sus bytes en cápsula/workspace y huella
   idéntica al mutarlos. No declara esos bytes secretos: basta --private.
   `r2_private_case_sensitive` crea objetos distintos por case, conserva los
   públicos de la otra grafía y distingue mutación privada de mutación pública
   en la huella. Exige un volumen sensible a mayúsculas. Los defaults literales
   y las tres pruebas PAO anteriores se conservan sin cambios.
2. Actores: `r2_handoff_actor` inicializa y cierra un estado propio y sustituye
   su log cerrado por un FIFO. La apertura bloqueante está antes de fstat y
   Popen en `_launch_supervisor`; al abrir el lector, el tipo FIFO determina
   NOT_CREATED sin crear supervisor. La cápsula nueva sólo propone un id:
   status positivo debe acreditar registered/starting/supervisor=null antes
   de encolar B. Su recibo real acredita registered/accepted/queued/uncertain.
   A no declara el hostname; B sí, con client distinto. El lector permanece
   abierto hasta recoger A y completar los dos fallos de lanzamiento. Se
   atribuye A únicamente con su recibo auténtico y se contrasta job/attempt con
   status. B exige never_created, evidencia válida y actores starting/finished
   del cliente A con hostname redactado, conservando el resto de la atribución.
   status/wait/evidence usan los secretos persistidos de B desde otro entorno.
   finally libera y recoge A aun ante fallo, acredita cierre antes de restaurar
   el log propio y conserva recursos si no obtiene identidad o terminalidad.
   No usa SQL, mocks de producto ni sleeps como prueba de registro.
3. Listas: `r2_colon_argv` y `r2_colon_content` exigen alias físico real en una
   posición no inicial de PATH/lista literal, en argv y en archivo admitido.
   Deben rechazar antes de crear BD, sin reescribir argv ni expandir código.
4. Coste: `r2_whole_argv` exige rechazo por longitud y profundidad antes de
   resolver el argumento absoluto completo. También exige detección textual
   del origen aunque aparezca después de un prefijo que supera la cota.
   Los tres métodos `r2_cost_` se sustituyen por entradas pequeñas: `/` más
   16.384 bytes Q (16.385 bytes), `/AAA` repetido 129 veces (516 bytes de base64
   textual con 129 barras) y `/Q\0\xff` repetido 40.001 veces (160.004 bytes).
   Cruzan respectivamente longitud, profundidad y cantidad. Deben devolver
   exit2/source_reference_limit, BD ausente y admisión refutada antes del
   registro, dentro de los mismos 15 segundos. No hay calibración ni RLIMIT_AS.
   El escáner valida todos los candidatos antes de resolver el primero; los
   casos largos/profundos no alcanzan realpath y el binario excede cantidad
   antes de efectuar sus stat. No se elevan las cotas 16384/128/40000.
   El positivo binario anterior de 8 KiB se conserva intacto. No se promete
   admitir todo binario de hasta 32 MiB ni se adoptan estimaciones del revisor
   como medición. Las cotas son de algoritmo; RSS/tiempo reales siguen pendientes.
5. Identidad no disponible: `r2_unobservable_loop` acredita ELOOP en un symlink
   propio y exige path_identity_unavailable por argv y contenido.
   `r2_unobservable_permission` acredita EACCES/EPERM con un directorio propio
   sin permiso que oculta un alias al origen; exige rechazo explícito tanto
   por argv como por PYTHONPATH. Requiere uid sin privilegios efectivos; no
   cuenta un entorno root que pueda atravesarlo. Restauración de permisos en
   finally. El producto no convierte esos tres errno en identidad ausente ni
   oculta errores inesperados. El rechazo conservador puede limitar entradas
   externas ordinarias y queda declarado en README.
6. Recibo posterior a registro: por inspección, result sólo existe después del
   retorno confirmado de _reserve. La frontera de errores abarca lanzamiento,
   relevo y cierre, devolviendo IDs/registered y admisión starting|queued con
   view=uncertain/exit3; no afirma estado terminal ni concede otro permiso.
   Las excepciones previas al registro no fabrican recibos. Se conserva el
   caso público anterior de copia de lector indisponible. No hay fixture
   pública determinista del timeout exacto al completar NOT_CREATED: el FIFO
   anterior acredita registro y fallo de lanzamiento, pero no una adquisición
   fallida de flock dentro del relevo. Esa dimensión permanece focalizada por
   inspección y requiere el procedimiento siguiente; no cuenta como ejecutada.
7. Descriptor/SQLite: la apertura falible del descriptor del escritor sucede
   antes de `_connect_locked`, bajo el flock exterior. Si falla, no se abrió
   SQLite; si falla el bootstrap, su cierre sigue bajo ese flock y se retira
   el descriptor sin transferir. No cambia cierre terminal ni copia BD/WAL.
   La focalizada de cierre de 12s se conserva. No hay fixture que fuerce EMFILE
   exactamente en la antigua ventana; el orden y la limpieza son inspección.

## Bases de fixtures y evidencia que debe preparar el coordinador

Cada harness valida la base antes de crear recursos dentro de ella; las rutas
existentes las suministra el launcher autorizado. `FLEET_TEST_RUN_DIR` es el
directorio físico real de la
corrida `remote-run/runs/<runId>`, propio y sin escritura de grupo/otros; no se
adivina desde HOME o runId. `FLEET_TEST_FIXTURE_BASE` debe ser un directorio físico
propio dentro de esa corrida, con toda su jerarquía bajo ella privada (0700).
Se rechazan rutas relativas, symlinks/componentes symlink y bases ajenas a la
corrida. No hay creación de base ni fallback a T compartido, HOME o `/tmp`.

`FLEET_TEST_CS_BASE` es la única excepción externa: directorio existente físico
del volumen sensible autorizado, del uid actual y modo exactamente 0700, sin
symlinks en su ruta ni ancestros. No tiene valor por defecto ni usa la variable
anterior `FLEET_CASE_SENSITIVE_TMPDIR`. No se crean bases, montajes ni volúmenes,
ni se reparan permisos. Para h3 Felix creó el volumen APFS sensible,
contenedor disk3, cuota 512 MiB, `/Volumes/pao-cs/fleet-tests` de cosmonaut 0700.
PAO comunicó certificado con Owners Enabled en preferences82 para la corrida h3;
no se inspecciona ni modifica aquí el volumen. Stat no demuestra enforcement si
Owners está desactivado. La suite no conecta para buscarlo. La cuota no es medición ni
garantía de espacio suficiente para las tres fixtures CLI seriales, sus
estados/cápsulas/workspaces/bundles, margen SQLite y los pequeños escenarios
de infraestructura h2/h3 con retenciones. No aloja la suite entera ni el estrés.

De las 95 conductas públicas, sólo los dos métodos cuyo nombre contiene
`case_sensitive_distinct` y el de cuenta/host usan esa excepción.
Cada uno crea con mkdtemp un subárbol 0700 exclusivo directamente bajo CS_BASE:
`fleet-cli-<runId>-py<major>-<minor>-pid<pid>-<azar>`. La identidad dev/inode,
uid/modo, método, corrida e intérprete se registran bajo la base ordinaria.
El azar de mkdtemp segrega también invocaciones/intérpretes del mismo runId;
no se reutilizan raíces preexistentes. HOME, proyecto y estado de esos tres
métodos permanecen dentro de su subárbol. Los otros 92 métodos CLI y la focalizada
siguen exclusivamente bajo FLEET_TEST_FIXTURE_BASE dentro de la corrida real.

Antes de cualquier submit externo se mide el comportamiento real del subárbol
con dos directorios propios CaseProbe/caseprobe. La segunda grafía debe estar
ausente tras crear la primera y luego tener otra identidad física/samefile falso.
Se guardan ambas identidades. Falta de variable, ruta/uid/modo inseguros o volumen
no sensible falla explícitamente, sin skip ni cobertura verde. Una precondición
fallida antes de admitir permite retirar sólo el subárbol recién creado, tras
revalidar identidad; su diagnóstico permanece bajo la corrida. Los aliases de la
base ordinaria siguen requiriendo case-insensitivity física. Sus skips heredados
no se modifican ni cuentan como cobertura; Mini debe acreditar ese filesystem.

Cuenta/host proceden siempre de pwd.getpwuid(os.getuid()).pw_name y
socket.gethostname(), sin simularlos ni modificar la cuenta. Se contrastan los
componentes relativos de state y los componentes de la raíz externa contra los
valores declarados, en bytes, para respetar la guarda productiva de credencial
en el state ABSOLUTO. El prefijo /Users/cosmonaut de la corrida sólo aloja
evidencia, no el state de este método y no produce skip. Una ruta externa que
también contenga un valor declarado falla la precondición, sin symlinks ni
sustitución de secretos. Se preservan las aserciones de submit/status/supervisor/
evidence, cápsulas y bundles; se exige además stdout con ambas líneas REDACTED y
los campos concretos de usuario/host redactados también en wait. La evidencia
pública debe ser válida y excluir ambos valores y sus digests.

La evidencia externa se guarda en un directorio nuevo 0700
`<FLEET_TEST_FIXTURE_BASE>/external-fixture-evidence-<azar>`, bajo runs/<runId>.
`setup.json` conserva las identidades y la medición inicial; cada
`cli-<n>.json` conserva comando, rc/stdout/stderr originales del CLI, sin imprimirlos.
Un timeout conserva returncode=null, plazo y los bytes parciales como campos hex
cuando subprocess los entrega en bytes; no se fabrica un rc ni un JSON de Fleet.
Antes de borrar se copian los recibos de admisión, diagnósticos propios,
supervisor.log, cápsulas y bundles publicados de los estados admitidos, con
ruta relativa/tamaño/SHA256, y se anotan IDs/refutaciones y retornos de clientes.
La enumeración usa scandir sin suprimir OSError, tanto en cápsulas como en
attempts y contenido recursivo de bundles. Sólo un lstat previo con ENOENT puede
registrar una ausencia opcional; ENOENT durante scandir es error. Una admisión
atribuida exige cápsula/payload/manifest e intento enumerados. El último estado
CLI auténtico terminado con colección sin bundle_seal_failed exige además
manifest del bundle; no se fabrica ni se exige bundle a todo caso refutado o
sin sellado. Rutas exigidas omitidas, lectura o copia incompleta impiden acreditar
snapshot y borrar originales; se conserva la evidencia ya disponible.
Las copias publicadas son archivos binarios privados numerados y conservan
también los bytes si falló la redacción. `external-fixture.json` es el snapshot
previo inmutable: fase, identidades/probe, mapa de origen, tamaños/hashes, IDs,
refutaciones y clientes. Snapshot, copias y respuestas CLI se crean O_EXCL y
O_NOFOLLOW, sin truncar ni reemplazar archivos previos. Sólo se acredita snapshot
durable tras sincronizar archivo, directorio de evidencia y su padre, que enlaza
el directorio nuevo en la base ordinaria. Ese protocolo termina antes del borrado.
Todos los archivos son privados 0600; pueden contener identidad real y valores declarados.
No se exporta el entorno completo ni se copia el ledger a la evidencia.

La retirada externa sólo ocurre después de settle y guard_admissions originales,
con clientes recogidos y ausencia de procesos/grupos propios acreditada por el
harness. Se revalidan base y raíz por path físico/dev/inode/uid/modo; se elimina
sólo esa raíz después de acreditar el snapshot. El resultado se escribe en otro
archivo, `removal-result.json`, sin volver a abrir el snapshot ni sus copias.
Sólo lstat con FileNotFoundError acredita ausencia observada; otro error no es
ausencia. `observed_absent` describe esa observación y el llamador sólo confirma
retirada al completar toda la persistencia sin excepción. Un recibo ausente,
parcial o una escritura/vaciado fallidos no acreditan cierre confirmado.

`cleanup-failure.json`, también separado y exclusivo, registra etapa, tipo,
mensaje/errno auténticos, snapshot durable o no, intento de retirada y estado
observado: no creado, retenido antes de intentar borrar, identidad cambiada,
presencia con contenido incierto tras intento, ausencia observada o no observable con causa. Incluye
identidad actual y si coincide con la original cuando puede observarla. Un fallo
previo al borrado impide retirarlo; un fallo de rmtree puede dejar datos parciales;
un fallo posterior puede encontrar la raíz ya ausente. No se afirma retención
integral ni se fabrica root_absent. La confirmación queda `unconfirmed` ante error,
incluso si un resultado parcial contiene bytes de una observación anterior.
La excepción original se vuelve a lanzar. Si persiste mal también este diagnóstico,
el error secundario se encadena a la excepción primaria y se intenta stderr.
Si write/flush de ese último canal falla, se entrega igualmente la primaria:
external_fixture_failure conserva causas/estado en memoria y
external_fixture_errors las referencias secundarias (Python3.9/3.14). Esto no
acredita diagnóstico durable ni salida completa cuando ambos canales fallaron.
El snapshot válido no se modifica por ninguno de esos intentos.

Un error de atribución/cierre impide borrar: se intenta snapshot retained_uncertain
y el recibo independiente de fallo, sin ocultar la excepción si también falla la
copia. Una precondición inválida antes de admitir sólo permite retirar su raíz
recién creada si se acredita snapshot y se revalida identidad; un OSError previo
conserva recursos propios. No se borra CS_BASE, montaje, hermano ni residuo histórico.
El cleanup de setup incompleto sólo aplica al recurso propio con el mismo protocolo;
no hay atexit ni reintento de retirada de las regresiones h2/h3 tras su error esperado.

Los recursos retenidos permanecen en `<base>/fleet-cli-*`: `admission-*.json`
contiene estado, exclusividad, ausencia inicial, IDs/refutación y respuesta;
`cli-json-*.json` guarda rc/stdout/stderr ilegibles; el caso sin JSON conserva
esa raíz intencionadamente. `closed-database-preparation.json` explica una
preparación no acreditada; `not-created-client.json` conserva PID/recogida en
el caso FIFO nuevo. Estado, cápsulas e intentos quedan junto a esos recibos.
La focalizada usa `<base>/fleet-lifecycle-*`, con `lifecycle-client.json` y
estado retenidos si no confirma recogida. No mover/ocultar cargas inciertas ni
borrarlas con refutación incompleta. El temporal de copia BD/WAL del producto
conserva su base `/tmp` validada y su límite plaintext; esta orden de fixtures
no modifica ese contrato.

### Estrés grande PENDIENTE de contención efectiva coordinada

El recorte autorizado retira de la suite funcional las tres calibraciones
RLIMIT_AS/320 MiB y los payloads grandes. No se convierten en skips ni se
cuenta la dimensión de coste grande como cubierta por las fronteras pequeñas.
Se conserva el procedimiento pendiente: preparar copias propias roja y verde,
coordinar con Mini una contención efectiva acreditada antes de lanzar cualquier
rojo potencialmente OOM, reservar su presupuesto y recoger cada cliente antes
del siguiente. No basta timeout, setrlimit/getrlimit ni RSS advisory como cota
dura Darwin. No se añade aquí contención, dependencia o volumen nuevo y no
se ejecuta el procedimiento sin resolver esa precondición con el dueño.

Entradas pendientes: slash-heavy `b'/Q' * (2*1024*1024)` (4 MiB), base64
`(b'/' + b'A'*63) * 65536` (4 MiB) y binario `b'/Q\0\xff' * (3*1024*1024)`
(12 MiB). Expectativa verde: exit2/source_reference_limit antes de resolver,
BD ausente y refutación válida, sin truncación/admisión silenciosa y con el
deadline de 15 s. Preservar plataforma/Python, entradas, rc/stdout/stderr,
tiempos y evidencia real de contención. El binario puede rechazar por cantidad
en ambos heads; registrar causa/coste sin inventar rojo obligatorio. La medida
de RSS no es esa contención. No se incluye un launcher de estrés funcional.
Esta pérdida de disponibilidad afecta también archivos ordinarios con base64
en notebooks/CSS/JSON; el máximo de 32 MiB no promete admitirlos todos.

### Procedimiento Mini del relevo fallido y del descriptor

Preparar estado y cargas propios por CLI y FIFO como en `r2_handoff_actor`;
inventariar admisiones antes de Popen y obtener status positivo y recibo auténtico.
Para el fallo posterior, un holder externo propio puede retener flock mientras
se libera el FIFO. Mantenerlo más de 10 s no prueba por sí solo que la operación
intentó adquirirlo: hace falta traza de plataforma atribuida al PID/inicio del
cliente que acredite entrada y timeout de esa adquisición, sin instrumentar
ni sustituir producto. El cierre terminal seguirá esperando al holder; liberarlo
explícitamente antes de recoger al cliente y exigir el recibo3 con IDs exactos,
registered, observation=durable_admission y view=uncertain, sin terminal inventado.
Consultar después, comprobar que no hubo comando ni nueva generación liberada
por el camino de error, y conservar los recursos si el slot queda incierto.
No borrar el estado de una admisión sin atribuir ni forzar su reconciliación.
Si no puede acreditarse esa frontera con observaciones de plataforma, devolver
la precondición como pendiente; no presentar una espera fija como reproducción.

Para el descriptor, usar subprocess focalizado con límite de descriptores propio
y traza de open/close/flock/SQLite, sin SQL fabricado ni sustituciones de funciones.
Acreditar EMFILE en la apertura del descriptor de estado y que no se abrió aún
una conexión SQLite original; distinguirlo de fallar antes durante directory/probe.
Comparar contra e469add3, donde el open ocurría después de abrir SQLite. El
intercalado exacto y su observación necesitan diseño/medición Mini; no existe
todavía script ejecutado ni se presenta la focalizada conservada como esa prueba.

### Selectores rojo/verde consolidados

Procedimiento pendiente, sólo tras APTO doble y coordinación Mini. Rojo desde
e469add3 y verde desde el nuevo head exacto, en copias propias. Antes del rojo,
trasladar los cuatro harnesses editados, con bases de corrida y coste pequeño:

```sh
git diff e469add3e663f9475cb7a75d0c56c2d1179b93fd "$R2_CANDIDATE" -- \
  fleet/tests/test_cli.py fleet/tests/correction_cases.py \
  fleet/tests/review_r2_cases.py fleet/tests/test_ledger_lifecycle.py | git -C "$R2_RED_COPY" apply
```

Con el turno FIFO original y los dos Python autorizados, usar `-k r2_private_case_aliases`,
`-k r2_colon_`, `-k r2_handoff_actor`, `-k r2_whole_argv`,
`-k r2_unobservable_` sobre `test_cli.py` en volumen case-insensitive, uid sin
privilegios y raíz temporal que no contenga el hostname declarado. En volumen
sensible usar `-k r2_private_case_sensitive`. Proporcionar las tres variables
de bases descritas arriba. Para las tres fronteras funcionales pequeñas:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k r2_cost_ -v
```

Rojo esperado estático: privados incluidos/huella variable, aliases posteriores
al colon admitidos y hostname del emisor A publicado en actores de B. Los nuevos
límites de ruta deben faltar en rojo. El caso pequeño de cantidad puede rechazar
en ambos heads; se conserva su causa sin atribuirle un rojo obligatorio.
Los casos de identidad deben distinguir el error
genérico anterior de path_identity_unavailable. El positivo de objetos distintos
debe pasar en ambos heads. Ninguna de estas expectativas ha sido ejecutada.
Para review4, preparar además copia roja de cde con sólo los cuatro harnesses
actuales y seleccionar `-k r4_`. Los cuatro literales y la ausencia de outcome
deben discriminar la corrección; los negativos de infraestructura y ciclo
pueden pasar en ambos heads. No ejecutar ni restaurar payloads grandes desde
estos selectores. Después ejecutar las 95 conductas públicas y la focalizada
conservada, con FLEET_TEST_RUN_DIR, FLEET_TEST_FIXTURE_BASE y FLEET_TEST_CS_BASE
explícitos según este contrato, más las cinco regresiones h2/h3 de infraestructura.
Un verde parcial con skips no acredita el conjunto de 101 métodos escritos.

## Correcciones integrales r3, todas pendientes de Mini

- Identidad física: seis casos `case_alias` exigen que la variante exista y que
  `samefile` y dispositivo/inode coincidan. Cubren argv absoluto, argumento
  `--file=...`, referencia literal en un archivo, PATH/PYTHONPATH/NODE_PATH,
  HOME declarado en ambos sentidos y sus ancestros, origen dentro del estado y
  exclusión de estado con grafía distinta en cápsula y huella. Sólo usan árboles
  propios. HOME lleva un FIFO propio para distinguir rechazo previo al inventario.
  Si el volumen no ofrece el alias, el caso declara skip: una corrida verde con
  esos skips no acredita B1. Mini debe ejecutarlos sin skip en una copia propia
  sobre volumen case-insensitive; repetir en volumen sensible el caso
  `case_sensitive_distinct_source_names`, que exige dos directorios de identidad
  distinta y admite la herramienta externa de la otra grafía. No se monta ni
  formatea nada desde la suite. La guarda HOME del uid usa el mismo comparador,
  pero su alias no tiene fixture pública segura: no se arriesga inventariar el
  HOME real si hubiera regresión. Su equivalencia permanece por inspección.
  Un caso positivo adicional conserva un archivo binario que empieza con `/`,
  contiene NUL/bytes no UTF-8 y un token mayor que un path válido: las comprobaciones
  de referencias no convierten bytes arbitrarios en una ruta completa de argv.
- Metadatos: LOGNAME/USER ficticios declarados, default client, recibo y
  status/wait/evidence desde otro entorno; se exige usuario del uid y ausencia
  del secreto/digest en publicación, cápsulas, bundles y supervisor.log. Otro
  caso declara como secretos la cuenta real del uid y el hostname, exige
  submitted_by.user/host y supervisor.host redactados y repite la inspección
  pública. Usa sólo sus nombres, sin inventariar HOME. En h1 esta fixture reside
  en su subárbol externo sensible propio; una ubicación incompatible con la
  guarda de credencial en state absoluto falla sin skip, según el contrato arriba.
  La inspección acredita que submitted_by se sanea antes de persistir y el lector
  usa los secretos guardados del emisor, sin depender del entorno de quien lee.
- Recibo durable: ocho trabajos legítimos con entorno privado de 48 KiB generan
  una BD cerrada de más de 256 KiB mediante CLI; no se fabrica un ledger con SQL.
  Tras una barrera running, otro cliente usa RLIMIT_FSIZE=256 KiB y SIGXFSZ
  ignorado sólo en su proceso. Se exige WAL de menos de la mitad del límite antes
  del envío. Su registro pequeño puede escribirse en WAL, pero la copia de la BD
  grande falla: un status nuevo bajo el mismo límite debe devolver exit 2 sin
  modificar el estado. El submit debe devolver IDs, registered/accepted true y
  observation=durable_admission/state=queued/view=queued con exit 0. Tras liberar
  se observa el mismo intento, una sola cadena de transiciones y stdout `once`.
  Si los tamaños no discriminan en una plataforma, falla la precondición; no
  cuenta como verde ni se aumenta el límite para ocultarlo. El caso heredado de
  supervisor perdido conserva queued/uncertain y exit 3. La carrera exacta de
  relevo antes de responder no tiene fixture pública sincronizada: el recibo
  declara admisión y no hace la lectura posterior por inspección.
- Cierre: `test_ledger_lifecycle.py` es una prueba focalizada, separada de la
  suite CLI. Un subprocess importa el Ledger real, abre una BD propia mediante
  el bootstrap real y anuncia la frontera de cierre. El controlador mantiene
  flock compartido durante 12 segundos, verifica que el dueño sigue vivo y que
  BD/WAL no cambiaron; libera explícitamente y exige cierre real de la conexión,
  salida 0, recogida y nueva adquisición EX. Conserva imagen tamaño/hash de
  BD/WAL/SHM existentes antes de liberar y después del cierre; durante SH exige
  invariancia también del SHM existente. No exige retirada de sidecars ni
  atribuye checkpoint a partir de presencia/ausencia. La prueba anterior 3.9
  llegó a closed/exit0 y falló sólo por WAL.exists; PERSIST_WAL y causa SQLite
  no se midieron. La lectura del cierre bloqueante no exige corrección productiva.
  Usa pipes/flock externos, sin mocks, hooks de
  producto ni timeout global modificado. Finalmente libera, recoge al hijo y
  sólo borra su raíz tras confirmar salida y todos los oráculos; conserva
  recibo/estado ante cualquier fallo.
  Acredita espera de cierre mayor al timeout ordinario, no la reproducción
  pública del intercalado exacto copia-BD/cierre-nativo/copia-WAL. Tampoco acredita
  errores nativos de flock/cierre ni muerte por señales; siguen pendientes.
- Residuo: SIGTERM/KILL puede dejar `/tmp/fleet-read-*` con copia plaintext de
  BD/WAL, entorno/argv privados. 0700/0600 protege frente a otros uid, no frente
  al mismo uid. No hay handler nuevo ni limpieza global, y no se mata un lector
  sólo para fingir limpieza de esa rama.
- Matriz144: se conserva el watchdog30 y la refutación de autoexit mediante done
  ausente por observación y released al cierre. Medir duración y margen en Mini;
  si se acerca o excede 30 segundos, devolver el rojo con su causa. Cualquier
  cambio futuro de sincronización debe conservar salida acotada y cleanup
  atribuible, sin limitarse a aumentar segundos.

Procedimiento histórico rojo/verde r3, **no ejecutado ni vigente tal cual**:
el harness histórico usa T compartido; antes de cualquier comparación futura,
incorporar las bases/recibos actuales bajo corrida y conservar todos sus mixins,
sin trasladar calibraciones ni payloads grandes. El padre coordina esa preparación.
La receta histórica de código era preparar en Mini dos copias propias
separadas, roja desde cb8f812 y verde desde el head exacto entregado, con ambos
worktrees originales y FIFO129 intactos. `INTEGRAL_RED_COPY` es la ruta de la copia
roja. Aplicar sólo el harness original r3, fijado a e469add3 para no introducir
imports del mixin consolidado sin su archivo nuevo:

```sh
git diff cb8f812ac40aa62de68a8503e6853ed750960775 e469add3e663f9475cb7a75d0c56c2d1179b93fd -- \
  fleet/tests/test_cli.py fleet/tests/integral_cases.py fleet/tests/test_ledger_lifecycle.py | git -C "$INTEGRAL_RED_COPY" apply
```

Ejecutar en cada copia con los dos Python autorizados por el padre, por el FIFO
original con turno propio; estos son selectores, no autorización de runtime local:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k case_alias -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k case_sensitive_distinct -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k literal_path_checks -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k metadata -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k durable_admission_receipt -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_ledger_lifecycle.py' -v
```

Rojo esperado por inspección: aliases admitidos/inventariados, LOGNAME publicado,
submit sin IDs tras fallo de copia y cierre que termina al agotar diez segundos.
El caso de objetos distintos debe seguir pasando donde exista. Verde esperado:
rechazos previos a BD, exclusión de estado/huella, metadatos saneados, recibo con
IDs y cierre bloqueado hasta liberar. Registrar skips, tiempos, errores y bytes
reales sin atribuirles aquí ejecución. El harness original tenía 68 conductas
públicas y una focalizada; el candidato actual requiere las 95 públicas,
las cinco de infraestructura h2/h3 y la focalizada completas. Los skips de aliases no cierran B1.

## Temporales del lector: oráculos conservados, h4 pendiente de Mini

Las lecturas bajo prueba en `reader_temp_cases.py` usan subprocess del CLI público, sin importar
Fleet, editar el ledger ni sustituir funciones. La validación independiente
de la copia del main file es la única consulta SQL añadida por h4. Cada consulta CLI es
un proceso nuevo, sin caché heredada de `tempfile`. Se retiran las tres variables
temporales y se introduce sólo la variable bajo prueba: TMPDIR, TEMP o TMP.
Cada una apunta al estado, a un subdirectorio privado suyo, a un symlink al estado
o a una cadena de symlinks al subdirectorio. Para cada combinación se consultan
status/wait/evidence con el job real y con un identificador desconocido.

Hay 72 consultas por fixture. La quieta se obtiene con submit/wait y cierre
propio y main file acreditados por el helper h4; admite las dos formas quietas
descritas arriba. La activa usa una carga
sin escrituras al estado tras alcanzar running y una barrera FIFO externa:
sólo el controlador libera al productor después de toda la matriz. Un watchdog
de 30 segundos termina con error 91, nunca con éxito; el marcador externo done
debe seguir ausente después de cada lector y decir released al finalizar.
No se amplían los deadlines heredados ni se usa el autoexit de seis segundos
para sostener la comparación. El finally libera incluso ante fallo y el harness
conserva sus comprobaciones de atribución y cierre antes de borrar recursos.
La fixture exige WAL con contenido y SHM presente, y observa running desde
la versión efectiva que incluye WAL. Tras liberar y cerrar exige una de las dos
formas quietas y valida el main independientemente, sin exigir unlink.

Antes y después de **cada** operación se compara el inventario recursivo completo,
incluido el propio directorio del estado: dispositivo, inode, modo, uid/gid,
nlink, tamaño, mtime/ctime en ns y hash de archivos, incluidos BD/WAL/SHM.
Crear y retirar un temporal o una sonda en cualquiera de los destinos altera
los metadatos del directorio aunque el inventario y los bytes finales coincidan.
La fixture primero crea y retira una sonda propia en el estado y en el
subdirectorio y exige detectar cada cambio antes de tomar las bases del lector.
No se compara atime, que puede cambiar por las lecturas de la propia fixture.
La capacidad discriminante de los timestamps también debe pasar en Mini.

Las entradas hostiles acreditan la elección de base independiente del entorno.
La precedencia de las guardas respecto de la primera llamada a `tempfile` se
comprueba también por inspección. No se modifica `/tmp` ni se fabrican montajes
para probar rechazo de una base insegura: esa rama, la administración root de
la jerarquía y su disponibilidad requieren evidencia de plataforma adicional.
No se promete resistencia frente a manipulación adversaria del mismo uid.

Selector histórico rojo/verde, **no ejecutado aquí ni vigente tal cual**.
También requiere incorporar primero las bases/recibos actuales bajo corrida;
no usar el harness histórico con T compartido. El coordinador debe preparar en Mini
dos copias propias desechables bajo el turno autorizado, sin alterar FIFO129 ni
los candidatos originales. `READER_CANDIDATE` es el head exacto entregado y
`READER_RED_COPY` una copia limpia separada de b5ca4987. Desde la copia candidata,
aplicar sólo el delta original del harness r2 sobre la copia roja:

```sh
git diff b5ca4987b00672b3d7eec9945b709e733efdefc9 cb8f812ac40aa62de68a8503e6853ed750960775 -- \
  fleet/tests/test_cli.py fleet/tests/correction_cases.py fleet/tests/reader_temp_cases.py | git -C "$READER_RED_COPY" apply
```

Desde cada copia, con el Python autorizado de la corrida:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -k reader_temp_environment -v
```

Rojo esperado por inspección: b5ca4987 crea y retira temporales dentro del estado,
con diferencias de mtime/ctime. No se modifica su runner. Verde esperado: la
copia candidata conserva inventario/metadatos y los exits 0/4/2 correspondientes.
Ambas expectativas necesitan ejecución, también los 95 casos CLI previos,
las cinco regresiones h2/h3 y la focalizada en los dos Python previstos.
Los casos antiguos no sustituyen esta matriz.

## Fixtures escritas, pendientes de Mini

- Entorno: rechazos separados de PYTHONPATH y PATH absolutos del origen, aliases
  de directorio/herramienta secundaria, alias con `..`, ancestros y escape relativo.
  Casos admitidos tras barrera: PYTHONPATH relativo, PWD ordinario transformado,
  PATH relativo desde cwd Git anidado y componente PATH vacío. Todos mutan o borran
  el origen después de admitir y exigen los bytes capturados al ejecutar. Se
  conservan el script sencillo sin Git y la selección externa PATH A/B heredada;
  otro caso conserva PYTHONPATH externo válido en cola.
  Las otras diez variables y separadores especiales tienen rechazos públicos.
- Raíces: HOME declarado y del uid, sus ancestros y `/` se rechazan antes de
  inventariar. Sólo se escribe en fixtures propias; un FIFO propio en HOME
  declarado discrimina guarda de alcance frente a rechazo posterior de especiales.
  Se prueba origen igual/dentro del estado y exclusión de estado dentro de proyecto.
  Se conservan rechazo de symlinks/componentes, especiales, submódulos y hardlinks
  copiados a bytes nuevos. No se inspeccionan árboles personales para la prueba.
- Semántica de paths: `--input package` y `--private package/private.data` desde
  cwd Git `package`, con salida `reports/result` relativa al cwd materializado.
  Se exige exclusión de hermanos/privados y contenido anterior a la mutación.
- Confidencialidad PAO: privados por defecto/ampliados fuera de captura y huella;
  secretos declarados por argv/entorno redactados; secreto no declarado en archivo
  normal que sí viaja como caso negativo explícito. Se comprueban cápsula, nombres,
  recibos, stdout/stderr, manifests, bundles y log del supervisor. Nuevos casos
  cubren client, selección, nombres de archivos, privados, artefactos y errores,
  incluidos el valor y su digest literal. Se excluyen de esa ausencia el ledger
  privado legítimo de argv/entorno y el workspace mutable escrito por la carga.
- Lectores: WAL activo y SHM presentes durante una barrera, wait timeout cero,
  status/evidence, sin alterar inventario, modos, inode, tamaño, mtime, ctime o hash.
  Para la preparación quieta se exige una de las dos formas h4 y lecturas
  exitosas sin cambiar la imagen completa. Estado inexistente, vacío, inseguro e incompatible se
  conserva sin bootstrap/reparación. El incompatible se obtiene de una BD propia
  cerrada: se cambia a 999 el entero big-endian público de `user_version`, offset
  60 del header SQLite; no se usa SQL para crear o modificar esa fixture. Los cuatro casos
  acreditan antes status finished con los IDs propios, recogen
  clientes y verifican ausencia de supervisores/grupos, adquieren EX no bloqueante
  del directorio y exigen forma quieta, header SQLite/Fleet y validación de la
  copia aislada del main según el contrato h4 anterior.
  Si no se acredita, informan causa de preparación y retienen imagen/recibo; no
  borran sidecars ni fingen un defecto del lector. Se conserva el error exacto
  `unsupported Fleet pilot schema; no migration` y la imagen íntegra antes/después.
  La prueba con WAL activo permanece separada, con su comparación sin relajar.
  FIFO129 corrió b5ca4987: sus fallos3.9 no prueban un resultado de cde ni del
  candidato h4. La corrida h3 y sus cuatro fallos tienen evidencia propia,
  descrita arriba; h4 todavía requiere Mini 3.9/3.14. Las preparaciones estaban
  fuera de fase B y ahora se corrigen sólo por la autorización h4; no se borran
  WAL/SHM ni se cambian errores para dar verde. La
  presencia/ausencia de sidecars por sí sola no acredita un checkpoint.
- Exclusión y FIFO: primeras cuatro admisiones simultáneas sobre estado ausente,
  con barrera de clientes y marcadores/gates distintos por comando. Sólo el
  primero llega a su barrera; los siguientes permanecen queued sin marcador
  hasta su turno. Se contrastan las secuencias y tiempos públicos. Otra fixture
  mantiene flock exclusivo de directorio desde un proceso distinto: submit agota
  el deadline de lock de 10 segundos sin BD ni carga; tras liberarlo puede ejecutar.
  El holder tiene autoexit de 20 segundos mediante select sobre stdin, suficiente
  para cubrir la llamada cliente acotada a 15 segundos, sin sleep fijo. Los
  deadlines existentes del harness no se amplían.
- Evidencia: artefacto válido, faltante, preexistente conservado, especial,
  symlink y credencial; payload ausente/truncado/reemplazado con bytes iguales,
  manifest ausente/truncado/ajeno. Dos intentos legítimos con bytes iguales siguen
  válidos. Se exige wait3 tras degradación, `recorded_outcome=succeeded` y
  transiciones sin cambios. Un comando exit0 con colección inválida produce
  incomplete. NeverCreated no fabrica logs.
- Streams: stdout y stderr simultáneos de 1 MiB; caso combinado con más de un
  pipe por stream y secreto de más de 4096 bytes en cada repetición, forzando
  cruces del límite de lectura del colector. Se contrastan todos los bytes
  redactados y las publicaciones. RLIMIT_FSIZE discrimina fallo de escritura y
  resultado incomplete con comando exit0; no acredita fsync ni cierre.

## Fronteras que siguen sin fixture pública discriminante

- Pérdidas exactas entre commit del claim, vínculo del supervisor, R, consumo de
  compuerta, Popen, persistencia del hijo, sellado del bundle y commit terminal.
  Se conserva la prueba de supervisor perdido después de running, con cola
  retenida y ausencia de duplicado, pero no acredita cada una de esas fronteras.
  El CLI no expone una barrera en ellas. No se agregaron hooks internos, mocks,
  SIGSTOP como admisión ni carreras temporizadas para fingir cobertura.
- Cambios durante captura de bytes/inventario/HEAD/índice. Hay revalidaciones por
  inspección y pruebas de bytes HEAD/index/working tree distintos, más mutación
  posterior al submit. No equivalen a una mutación sincronizada dentro de captura.
  Una carga grande y un mutador concurrente dependerían de una ventana de carrera;
  no se presentan como fixture discriminante.
- Fallos independientes de fsync/cierre de streams, artefactos, bundle o ledger.
  No hay fixture pública determinista sin inyección interna o cambios de host.
  RLIMIT_FSIZE cubre sólo escritura. Durabilidad ante pérdida de energía tampoco
  está acreditada.
- Contrato de contexto desconocido persistido: rechazo explícito por inspección
  antes de compuerta/spawn, sin fixture CLI que produzca tal ejecución sin editar
  internamente el ledger. No se inventa runtime ni migración de contexto anterior.
- Disponibilidad al superar 256 MiB en un archivo de ledger: cota inspeccionada
  y documentada, sin fixture pública económica que genere esa acumulación mediante
  el CLI. Tampoco se midieron RSS, latencia ni rendimiento del redactor.

Estas omisiones son distintas de los casos ya escritos que sólo esperan ejecución
autorizada en Mini. Falta acreditar allí soporte de flock entre procesos y sobre
directorios, WAL/copia/checkpoint, fsync de archivos/directorios, identidades de
procesos/grupos y timestamps de evidencia, además de ejecutar todas las conductas.
La tabla de entorno tiene la cota exacta del README; no se promete resolver código
arbitrario, dependencias externas recursivas, aliases mutados tras admisión ni
resistencia frente al mismo uid. Este documento no declara APTO ni cierre.
