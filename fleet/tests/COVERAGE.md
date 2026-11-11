# SEN-3821, harness h5 sobre h4 y producto e999 intacto

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
H4 valida además una copia aislada del main file; h5 prepara el incompatible
en otras copias privadas, según el contrato siguiente. Cada submit se
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

## H5: incompatible derivado de un main rollback comprobado

Autoridad: Felix vía PAO075b50e8, decisión ~23:05Z del 2026-10-26 y brief
`SEN-3821-harness-h5-rollback.md`, SHA256
`e0845def0e802ed907e9009868e6004a9bee05f14aaa003cb5594ab3228db573`.
Base `4083b397794df995abf2b286b93fa87245eb7d3c`; producto e999 intacto.
H5 conserva **101 métodos escritos, cero ejecutados en esta autoría**; cambia
sólo la preparación del caso incompatible y su documentación. No añade métodos,
skips ni cambios a `closed_database_bytes` o a los otros tres casos WAL.

La suite h4 midió 100 pass/1 fail en Apple3.9 y 101 pass en3.14. La sonda A,
`D/reports/SEN-3821-incompatible-probe-A-resultados.md`, SHA256
`23729dead80845fbec3a5de5a1b0bb4a2f97be8afdde7e6b02aa006afb098a68`,
localizó el fallo Apple en application_id: connect/foreign_keys pasan, pero el
main-only con header WAL2/2 no es abrible en ro antes del schema check. El
genérico de producto era el saneamiento previsto. No hay código numérico3.9
medido; SQLITE_CANTOPEN es clasificación del custodio. H3 tampoco pasó ese caso
en3.9: falló antes por sidecars. Estos resultados no son ejecución de h5.

`incompatible_rollback_state` recibe los bytes acreditados por el helper h4 y
el resultado terminal del wait auténtico. Crea `incompatible-preparation/state.db`
nuevo 0600 bajo directorio propio0700, sin copiar sidecars. Sólo ese archivo se
abre rw: comprueba modo inicial wal/delete, luego wal_checkpoint(TRUNCATE), con
retorno exacto (0,0,0) para WAL (sin busy ni frames pendientes), o (0,-1,-1) para
un main ya DELETE. Después exige retorno delete de journal_mode=DELETE y cierre
SQLite satisfactorio. WAL0 no sustituye estos retornos ni la validación siguiente.

El main cerrado debe tener header SQLite/FLT1/schema3 y bytes18/19=1/1. En
`incompatible-validation/schema-3.db`, copia independiente0600, abre mode=ro
**sin immutable**: foreign_keys/application_id/user_version, modo delete,
integrity_check=ok, job/attempt finished, outcome persistido e historia por
seq/IDs/estados/tiempos iguales al wait; no añade actor a la proyección h4.
Exige historia no vacía terminada en finished e imagen íntegra invariable de la
validación. Sólo entonces cambia60:64 a999 y repite esas comprobaciones sobre
otra copia `schema-999.db`, antes de escribir el fixture incompatible0600/0700.
Así discrimina también la apertura normal ro del derivado, sin fabricar18/19 ni
sembrar tablas. Contrasta además la imagen completa del estado original.

El bucle unsafe/empty/incompatible/absent conserva sus doce lecturas
status/wait/evidence, rc2, literal EXACTO `unsupported Fleet pilot schema; no migration`
e imagen íntegra antes/después. Un fallo de preparación marca retención antes
de observar/persistir diagnóstico: copia/imagen/retornos/causa quedan en la raíz,
con `incompatible-preparation.json`0600 si puede escribirse. La misma primaria
se relanza; errores de cierre, imagen o informe quedan encadenados/en memoria,
sin éxito ni diagnóstico durable inventados. El cleanup existente no borra la raíz.
N5 comprende **todas** las copias nuevas: ningún main de preparación/validación/
incompatible, ledger/WAL/SHM/closed-main ni entorno privado entra en el paquete,
independientemente del nombre. No se tocan los seis nits h4 ni el lector producto.
H5 se cerró con reviews Astra high + Opus high, APTO doble, veto PAO y suite
Mini h5 (turno 340, 101/0/0/0 por stack), y se mergeó en el PR 31.

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
  cerrada: h5 convierte sólo una copia propia a rollback y valida su main según
  el contrato anterior; después cambia a 999 el entero big-endian público de
  `user_version`, offset60 del header SQLite, sin SQL de sembrado. Los cuatro casos
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


---

# SEN-3820 harness r8 — matriz pública, cero ejecución del candidato

Inventario obtenido por texto, sin discovery/imports: **64 métodos** en un único
TestCase `FleetCLI`:16 base en `test_cli.py`,28 en `Core3820Cases`,20 en
`Operator3820Cases`. Se conservan íntegros los62 métodos de d5 (16+28+18), sus
contratos; r4 añadió dos y r5 corrigió sólo el oráculo temporal del
primer método r4, sin añadir métodos. R7 conserva r6 y corrige sólo retención
temprana, fotografía C08 y respuesta lectora C07, sin nuevos métodos.
R8 añade sólo la campaña acotada C07, preservando test_cli/operator byte a byte.
Los53 r1, cinco r2, cuatro r3 y refuerzo C02
quedan conservados. Discovery futuro sólo `test_cli.py`, según README.
**34 filas padre =26 definidas +6 parciales/condicionales +2 positivas abiertas**.
Los3 requisitos adicionales no cambian esa partición. No se cuentan métodos como
subcasos ni código escrito como cobertura runtime. R7/r8 siguen sin ejecutar; los
resultados históricos de c63 se distinguen al final.

Cada nombre siguiente es un método real completo, con prefijo `test_`. C reside en
`core3820_cases.py`, O/A/B en `operator3820_cases.py`. Recursos comunes: directorio
físico propio0700 `FLEET_TEST_RUN_BASE`, estado/proyecto/HOME propios, CLI en procesos,
JSON/códigos originales, transiciones y barrera con watchdog fallido30s. El comando
counted escribe/fsync un contador externo fuera del origen capturado. IDs iguales
por sí solos no prueban una ejecución. Sólo procesos de fixture reciben señales.
Ninguna prueba importa Fleet, escribe SQL o inyecta respuestas/cierre en producto.

| Fila / clasificación frozen | Método (prefijo test_) | Subcasos y oráculo escrito; límite |
| --- | --- | --- |
| C01 / definido | c01_equal_concurrent_admission_and_external_single_entry | Dos clientes con barrera externa de inicio: new/replay, IDs/admission/input originales, contador1, wait0 tras final. Carrera acotada; no acredita interleaving interno determinista. |
| C02 / definido | c02_different_concurrent_content_conflicts_without_second_entry | r2: correspondencia argv/etiqueta/CompletedProcess/respuesta0/5; existing original y rechazo explícito. Contador externo exactamente [etiqueta ganadora] durante running y al terminar. Stdout y artifact winner.txt publican sólo esa etiqueta; evidence/manifiesto y hashes ligados a job/attempt/input ganadores. Carrera acotada, sin ganador predeterminado ni intercalación interna acreditada. A5 corrige el oráculo, no demuestra ejecución errónea r1. |
| C03 / definido | c03_discarded_submit_stdout_recovers_by_key_and_replays_once | Toda salida submit a DEVNULL; otro status K atribuye admisión, replay exacto conserva IDs, contador1. ACK perdido, no caída en commit. |
| C04 / definido | c04_lookup_after_origin_removed_and_candidate_rejection_is_scoped | Origen retirado tras running; status/wait/evidence con env diferente resuelven original; nuevo candidato imposible2/not_evaluated, asociación conservada. |
| C05 / definido | c05_finished_success_and_failure_replay_never_execute_again | Exit0 y7 separados: wait0/1 con complete; replay conserva recibo y contador1. |
| C06 / definido | c06_lost_supervisor_and_empty_group_do_not_prove_terminal | SIGKILL a supervisor propio tras running, gate termina comando, ausencia de grupo observada; A incierto, B bloqueado owner_uncertain, replay0, wait/recover3, sin otra entrada. |
| C07 / definido, muestra condicionada a preparación | c07_sigint_wait_only_interrupts_reader | Comando running en barrera, mismo shim SIG_DFL/marcador, importtime sólo importación y SIGINT real al lector propio. Campaña≤3 intentos/10s monotónicos TOTALES, incluida recogida y comprobaciones del último; communicate≤5s y≤remanente, todas las esperas acotadas por ese remanente. Sólo130/client_interrupted/unknown con effects unknown oread_only, JSON válido, cliente recogido e invariantes habilita reintento si queda cota. Sólo wait/read_only recogido130 con invariantes acredita identificación en main, no entrada exacta al bucle wait. Imagen pública/contador1 contra original y anterior cada intento; canales íntegros/UTC/recogida/faltantes antes de asserts. Unknown nunca PASS/cobertura; agotamiento BLOCKED de preparación, nunca skip verde/defecto de producto; registro total/unknown_count/UTC de señales. Sin demora50ms ni otra ayuda de planificación. Watchdog30s/cleanup/cierre original intactos. |
| C08 / definido | c08_client_group_loss_preserves_original_supervisor | Mismo shim exclusivo de clientes C07/C08, wrapper en sesión propia. self.ready("c08", ready) exige marcador y running público durable antes de fotografiar admitted y enviar SIGHUP real al grupo; marcador y retorno por SIGHUP en5s. Supervisor original, running, reserva/transiciones y contador1; settle10s. Sin preexec_fn ni política global. |
| C09 / definido | c09_timeout_only_for_safe_pending_and_uncertainty_precedes_it | Running da4; dueño perdido da3 antes de timeout0. Ventanas terminal/publication/release pendientes sólo si se alcanzan externamente (C20/O07); no acreditadas aquí. Baseline wait registra/reintenta únicamente la ventana finished/publication pending para esperar complete. |
| C10 / definido | c10_enqueue_only_survives_and_two_recoverers_claim_once | Enqueue-only queued seguro y dispatch_not_requested; replay no despacha; dos nuevos recoverers, un claim durable/contador1. Se tolera3 transitorio de un observador concurrente starting sin vínculo. |
| C11 / definido | c11_results_survive_new_coordinator_processes | Exit0/7, procesos nuevos status/evidence/recover; comando/huella/IDs persistidos, wait0/1, contador1. |
| C12 / definido | c12_recover_preserves_live_owner_and_does_not_adopt_lost_owner | Dueño vivo acreditado permanece, recover no claim; variante supervisor perdido retiene/recover3. Pérdida del coordinador separado también en C08. |
| C13 / definido | c13_effective_bytes_catalog_mode_head_and_index | Repo propio: bytes, catálogo y bit exec distintos dan5; sólo índice diferente replay; commit vacío cambia HEAD y da5. Contador externo no aumenta. |
| C14 / definido | c14_private_exclusion_secret_equality_and_undeclared_negative | Privado mutado replay; credencial ficticia cambiada5 sin valor/digest público; archivo ordinario no declarado cambia conflicto y sí viaja al log; stdout/argv con secreto declarado se redactan. |
| C15 / definido | c15_argv_environment_outputs_declarations_client_and_pwd | Variaciones separadas argv/PATH/volátil/presencia env/artifact/credential-env/client dan5; sólo PWD derivable replay; PWD declarado credencial cambiado da5. |
| C16 / definido | c16_precommit_rejection_does_not_bind_key | Autoridad inicializada; input inválido2/candidate false/not_evaluated, status K not_found con revisión; después K válido new. |
| C17 / definido | c17_absence_is_revision_and_authority_scoped | Not_found antes de envío y admisión posterior con revisión mayor; lector no inicializa otra autoridad; mismo K nuevo en otra autoridad tiene UUID distinto. La ausencia no pronostica una carrera futura. |
| C18 / definido | c18_missing_artifact_signal_and_positive_never_created | Exit0 sin artifact: cierre original, collection incomplete, retained, wait/recover3 y replay histórico; señal propia SIGKILL conserva signal/no exit137 y wait3 aun released; ejecutable inexistente: never-created positivo, bundle vacío explícitamente N/A, complete/failed/wait1. |
| C19 / definido | c19_post_completion_payload_loss_does_not_rewrite_history_or_reacquire | Retirar artifact después de complete; sustituir manifest de un segundo job por el primero, conservando copias: recorded succeeded, outcome incomplete/evidence false, wait3 y released permanece. Recover no readquiere capacidad. |
| C20 / condicional BLOCKED | c20_real_quota_failure_at_terminal_collection | R6 prepara sólo setup pao-cs certificado, raíz propia e inventarios; termina BLOCKED antes de admisión/gate/relleno porque falta discriminante aprobado de colección/publicación terminal y ack terminal propio. ENOSPC/EDQUOT del relleno, gate fallido o incertidumbre no acreditan esa frontera. Una implementación futura aprobada debe conservar retención/wait/recover3/contador1 y restaurar exclusivamente relleno propio atribuido en finally. Ningún PASS/skip por setup. |
| C21 / ABIERTO positivo | c21_host_backend_rejected_without_claiming_positive_generation_coverage | Sólo submit backend host-reservation@1 rechazado2/backend_unsupported/not_evaluated, sin crear estado. Falta integración capaz de entregar/procesar cierre ordinario G con H activo. No API simulada ni test positivo. Nota SEN-5649 3b: el estímulo usa ahora `unknown-backend@9`; host-reservation@1 se admite sólo con autoridad efectiva y queda `queued` (`test_host_reservation_cli`); la ejecución host sigue abierta. |
| C22 / definido | c22_readers_and_replay_leave_original_authority_unchanged | Running en barrera y luego supervisor perdido con grupo terminado; status/evidence/wait/lector fallido con TMPDIR/TEMP/TMP apuntando a estado y alias; inventarios sin atime idénticos en ambas vistas. Replay con env original conserva transiciones, payloads originales y contador; permite sólo preparación propia atribuible. |
| C23 / parcial/condicional | c23_authentic_incompatible_cli_states_and_retention_preconditions | CLI auténticos2/3 en copias propias crean estados activos y cerrados; nuevo status por JOB/K, wait, evidence, recover, submit con/sin K rechazan2/unsupported_schema con inventario sin cambios. Historial auténtico>7d consulta K y fecha real; sin reloj inyectado/SQL. Faltan copias CLI provistas e historial auténtico; edad uncertain separada no acreditada por un historial finished. |
| C24 / parcial | c24_public_queued_and_running_crash_campaign_preserves_one_permission | Queued público, recover, running, replay y contador1; supervisor propio perdido tras running retiene/recover3. C08 añade muerte de cliente. Claim/vínculo/compuerta/Popen/fsync/commit exactos no controlados ni cubiertos por esos estímulos. |
| O01 / definido | o01_positive_local_resolution_keeps_failed_evidence_and_drained_fifo | Cierre original real con artifact faltante; B queued; drain/assess/release auditados, actor nativo, proof, errores e incomplete conservados, drained y B sin entrar. Sólo reopen+recover permite contador B1. |
| O02 / definido | o02_empty_group_without_original_terminal_and_self_assertions_cannot_release | Supervisor perdido y grupo terminado; assess insuficiente3/eligible false, proof inventado2, proof negativo3 y --proof-file JSON rechazado2. PID/TTL/done/contención autoafirmados no autorizan release. |
| O03 / condicional | o03_reason_and_declared_actor_rejections_with_real_os_actor | Motivo ausente/vacío/espacios/control/largo y flags --actor/--uid rechazados2; USER ficticio no cambia uid/cuenta/birth nativos. Otra cuenta real y denegación real de observación nativa requieren ejecutores externos preexistentes: no hay fixtures equivalentes ni cobertura positiva de esos negativos. |
| O04 / definido, operador | o04_new_old_generation_release_is_processed_and_rejected_before_proof | G liberado, H running, C queued, drain nuevo; nueva operación para G da5/generation_mismatch, validation_processed auditado, H intacto. Replay G devuelve historia sin tocar H; C sólo tras cierre/reopen/recover. No cierra C21. |
| O05 / definido | o05_operation_binding_replay_conflict_and_concurrent_release | Dos releases mismo ID convergen new/replay con receipt igual; motivo distinto5/audit; dos IDs distintos de otra reserva producen committed/already_released, un release efectivo. |
| O06 / condicional BLOCKED | o06_real_quota_failure_cannot_commit_capacity_without_audit | Dos subTest de setup propio, release y reopen, ambos BLOCKED antes de admisión/relleno. No se simula assess/release. Falta discriminante aprobado de fallo real en commit tras assess y tras release respectivamente; rc3/persistence_unconfirmed/operation_not_found puede venir de apertura y nunca basta para PASS. Mantiene como obligación futura rechazo2/3 y status not_found/retención o committed con auditoría completa/capacidad/control correlacionados, contador1 y recuperación. No inyección ni observación interna fabricada. |
| O07 / parcial | o07_lost_stdout_recovers_each_atomic_operator_binding | Descartar stdout de los4 verbos, status y replay recuperan receipt; segunda asignación terminal, SIGKILL a clientes propios después de importtime y consulta/repetición explícita. Registra si señal se envió y binding observado. Frontera exacta intención/commit/fsync no probada; importtime/ACK no se presentan como tal. |
| O08 / definido | o08_only_explicit_current_epoch_reopen_allows_dispatch | Release incompleto sin reopen; evidence/recover no abren ni B entra. Reopen explícito; nuevo drain D2, replay viejo devuelve historia, nueva operación D1 da5. Reopen D2+recover permite B1. |
| O09 / definido | o09_old_epoch_proof_and_cross_authority_proof_cannot_change_current | Tras release/reopen/drain nuevo, misma asignación ya libre responde already_released sin repetir release. Proof de otra autoridad contra destino todavía retenido da2/proof_invalid; proof propio sí libera. Mismatch prioritario con H en O04. Proof_stale exige cambio relevante acreditado, no se simula alterando revisión. |
| O10 / ABIERTO, fuera3820 | o10_host_side_operator_backend_is_explicitly_unsupported | Sólo operator_backend_unsupported2 auditado, sin drenar ni soltar. Positivo host-side depende de dueño/interfaz/prueba autorizada futura3822/3823; sin fixture/API ficticia. |

## Requisitos adicionales

| ID | Método (prefijo test_) | Oráculo y parte pendiente |
| --- | --- | --- |
| A-W1 | a_w1_live_drain_blocks_reopen_and_ordinary_supervisor_handoff | A running causal, B queued → drain D → reopen prematuro3/reopen_blocked → cierre ordinario A completo/released, B permanece queued durante D → reopen nuevo+recover → B una vez; audit/transiciones/counters. A6 diferido: released no acredita que el supervisor haya recorrido el relevo antes de reopen; no cierra cobertura causal de esa guarda. |
| B-W1 | b_w1_sigint_dispatch_campaign_records_actual_observable_window | Tres submits propios, observar admisión/estado/vínculo, SIGINT sólo si cliente vive, salida0/130 y admisión durable; gate, terminal/released y contador≤1. Registro explícito exact_pre_spawn_proven=false. Starting/unbound no prueba que Popen no ocurrió. Falta control externo causal del hueco preciso claim→spawn, sin hooks/SQL/sleeps que lo finjan. Política de producto sí está implementada y descrita en SEN-3820.md. |
| B-W2 | b_w2_atomic_local_binding_not_found_committed_ack_loss_and_conflict | Status not_found, drain con stdout perdido, committed/audit, replay receipt igual, cambio5; recover operator_pending vacío y drained, segundo ID already_drained0 misma época. Rollback/cuota en O06 y muerte acotada en O07; no pending durable en modelo local. |

## Regresiones r2 adicionales

Los nombres llevan prefijo `test_`. La trazabilidad estática exacta contra0b,
incluida la diferencia de A5, está en [SEN-3820-r2.md](../SEN-3820-r2.md).
Estas cinco adiciones no reclasifican las34 filas contractuales.

| Hallazgo / módulo | Método | Subcasos escritos y discriminante |
| --- | --- | --- |
| A1 / core | r2_a1_complete_selections_validation_and_true_equivalences | 6 estímulos tras setups: punto+foo5 sobre bytes iguales; automático/punto duplicado/reordenación con duplicados replay; punto+missing y punto+.env2/not_evaluated. Original/contador intactos. 4 invocaciones subTest. |
| A2 / operator | r2_a2_conflict_audits_entire_denied_request_without_rebinding | 3 conflictos con otra asignación auténtica (release/assess/reopen), todos los selectores/prueba/época distintos; request íntegra/redactada y5/efectos nulos. Motivo secreto2 auditado sin binding;3 backends malformados2 sin auditoría; replay G conserva receipt ante H retenida. 6 invocaciones subTest,8 estímulos de estas categorías. |
| A3 / operator | r2_a3_pages_exhaust_divergent_operations_and_audit | 1 recorrido: una operación+3 conflictos sólo audit, límite2, ambos cursores, orden/identidades/listas exactas, avance y fin; página vacía, selección exacta e inventario iguales. |
| A4 / core | r2_a4_reader_open_and_selector_failures_are_read_only | 19 estímulos:4 lectores ×2 aperturas (ausente/privacidad insegura),3 lectores ×3 selectores erróneos, operation_not_found y paginación inválida. Todos2/read_only/sin observation; no crear autoridad, inventario idéntico. 17 invocaciones subTest. |
| A4 / core | r2_a4_real_directory_lock_distinguishes_readers_and_writer | 4 lectores concurrentes bajo flock EX externo causal devuelven2/read_only/state_lock_timeout;1 escritor distinto conserva3/unknown. Gate/finally, recursos propios, watchdog60s, ausencia de binding e inventarios idénticos. Ningún ledger escrito por fixture. |

Cinco métodos nuevos y27 invocaciones `subTest` nuevas por expansión estática de
bucles; recorridos/aserciones sin subTest no son métodos adicionales. Todos escritos,
cero ejecutados. Esquema incompatible sigue en C23 condicionado a CLI auténticos;
corrupción SQLite real/EACCES de apertura no tienen nueva fixture en r2. Su manejo
se inspeccionó; no se fabrican bytes de ledger para cerrar esos escenarios.

## Regresiones r3 adicionales

Los cuatro métodos completos llevan prefijo `test_`. La respuesta individual,
fuentes127 y semántica pública están en [SEN-3820-r3.md](../SEN-3820-r3.md).
Los58 métodos anteriores y las34 filas padre conservan sus setups y oráculos.

| Hallazgo / módulo | Método | Subcasos escritos y discriminante contra127 |
| --- | --- | --- |
| R2-A01 / operator | r3_a01_new_denial_sanitizes_all_selectors_and_preserves_raw_binding | Credenciales ficticias de forma válida persistidas por submit enqueue-only; lector sin ese env; operation-id ausente y release NUEVO2/unknown_job. Ausencia de valores en ambos canales, target anidado/drain/proof redactados, generation int7 y atribución conservadas; receipt exacto, replay2 y recorrido completo limit1 iguales a listado grande (2 operaciones/5 eventos). Cambiar sólo un secreto por otro da conflicto5 pese a igual redacción; reason/ID secretos dan2 auditado sin binding, control no secreto conserva generation_mismatch5. Job/control/capacidad/contador sin efectos.127 publica los selectores crudos en la primera respuesta nueva: falla el oráculo de ausencia antes de comparar receipt. 2 invocaciones subTest de rechazo por credencial. |
| R2-A01 / operator | r3_a01_readers_resanitize_history_with_later_persisted_declarations | Productor CLI auténtico guarda una denegación antes de declarar sus selectores ficticios como credenciales; segundo submit enqueue-only los declara. Status exacto, página limit1/fin y replay deben ahora sanear esa historia sin reescribirla, preservando receipt atribuible, revisión y listas.127 devuelve history cruda: falla ausencia en status exacto. No SQL/mocks/ejecutable antiguo ni objeto guardado inyectado. Sin subTest. |
| R2-A02 / core | r3_a02_empty_inventory_automatic_and_dot_replay_without_dispatch | Origen realmente vacío y sin ancestro Git: automático admite input hash de []; punto, punto duplicado y automático replay0 aunque se omita enqueue-only. Punto+missing/.env/.././ sigue2/not_evaluated; sólo .env excluido mantiene inventario vacío y equivalencia. Otra declaración private conserva conflicto5 aun con mismos bytes vacíos; job/admission/input/safe-pending/control/transiciones intactos, sin contador/supervisor.127 rechaza el primer replay punto2/input_not_admitted. 9 invocaciones subTest (3+4+2). |
| R2-A03 / operator | r3_a03_both_cursor_ranges_return_json_without_mutation | Autoridad válida, una operación/dos eventos. Ambos flags con -1, int64max+1 y uint64max, tanto enumerando como por ID: JSON una línea2/invalid_pagination/read_only, sin traceback/auditoría/observation/mutación.0, max-1 y max válidos; exacto conserva receipt, default audit y streams independientes conservados, páginas normales y fin.127 desborda SQLite con max+1 enumerado en cualquiera de los flags: traceback1 sin JSON. 18 invocaciones subTest (12 inválidas+6 válidas); además recorrido independiente de2 páginas sin subTest. |

Cuatro métodos nuevos,29 invocaciones subTest nuevas por expansión estática
(A01=2, A02=9, A03=18). Son casos escritos; **cero ejecutados**, incluido baseline127.
Se mantienen las27 invocaciones subTest añadidas en r2 y todos los demás casos.

## Regresiones r4 adicionales — R3-A01

Dos métodos añadidos en r4 a `operator3820_cases.py`, ambos con prefijo `test_`, más el
helper de recorrido `r4_operator_pages`. R5 sólo modifica el primero de esos métodos.
Regla pública y fuentes completas en [SEN-3820-r4.md](../SEN-3820-r4.md).
El discriminante se deduce de leer d5 y el oráculo; no hay baseline FAIL ni PASS
medidos. La ausencia global sólo se exige para selectores secretos; credenciales
que coinciden con protocolo se comprueban por campo y procedencia.

| Método | Fixture CLI válida, oráculo y discriminante estático contra d5 |
| --- | --- |
| r4_a01_denial_literals_and_copied_data_before_and_after_declaration | Origen físico vacío noGit, counted externo, submit enqueue-only con declaraciones explícitas de fleet-job-api@1/unknown_job/no_capacity_change y seis selectores válidos. Dos subTest: declaración antes de la NUEVA denegación y después de historia auténtica (denegación+conflicto por CLI). Release de job inexistente llega a unknown_job antes de drenado/prueba. Respuesta/recibo/evento/replay/exacto/listado y todas las páginas limit1 conservan api/error.code/effects/decision/status/action/binding; target/drain/proof siguen redactados y generation int7. Un backend copiado igual al literal api se redacta en request del conflicto. Cambiar sólo j_A por j_B da5, aunque ambas proyecciones sean iguales; binding/receipt originales intactos. ID y reason copiados iguales a unknown_job se rechazan y sus cuatro campos auditados muestran marcadores, sin binding. Lectores no reescriben historia; control/capacidad/transiciones/contador/supervisor sin efectos. En d5, el primer public nuevo (antes) o exacto tras declaración (después) pierde api; también decision/error/effects. El oráculo de literal falla por inspección. |
| r4_a01_effective_control_preserves_committed_and_historical_replay | Origen vacío noGit, submit enqueue-only declara committed y fleet-job-api@1; lector sin esas variables. Drain efectivo confirma control drenado y receipt/status/decision committed; reopen separado abre. Replay del drain conserva historia y control actual abierto. R6 separa inventario íntegro de lectores y comparación durable del replay descrita abajo; dos operaciones/dos eventos committed. Reason copiado committed se rechaza y redacta en evento/request, conservando códigos/efectos; no drena ni despacha el holder. En d5 ya la respuesta drain pierde api/status/decision; sin la colisión api también fallan los asserts committed. No se mide runtime r6. |

Dos métodos, **2 invocaciones subTest nuevas** por expansión del primer método;
el segundo no usa subTest. Se conservan62 métodos, cuatro adiciones r3/29 subTest,
cinco r2/27 subTest y las34 filas26/6/2, más A-W1/B-W1/B-W2. **0 ejecutados**,
incluido d5. Una historia cuyo productor ya borró un literal no se repara aquí;
el caso posterior declara credenciales tras producir historia auténtica intacta.

Corrección R4-A01/F1 en [SEN-3820-r5.md](../SEN-3820-r5.md): evento y recibo tienen
capturas de tiempo independientes. El primer método valida por separado sus tiempos
UTC con microsegundos en esta fixture y conserva cada valor desde su propio origen,
antes/después de declarar credenciales, en replay/status y todos los recorridos de
páginas pertinentes. No exige igualdad, orden ni delta entre relojes. Mantiene las
igualdades de destino/decisión/acción/actor/motivo/revisión, enlace audit_seq y todos
los asserts posteriores de privacidad, binding y ausencia de efectos. Los64 métodos
y los2 subTest r4 se conservan, **0 ejecutados**; N2 de credenciales cortas sigue diferido.

## Prerrequisitos externos y registro futuro

No se han creado ni comprobado recursos de ejecución durante esta entrega.
La futura corrida debe conservar entorno/certificación/head e intérprete exactos.
Ausencia de un prerrequisito falla explícitamente; no hay skips que sumen cobertura.

| Condición | Variables/entrada futura | Parte no acreditada |
| --- | --- | --- |
| Directorio de corrida | `FLEET_TEST_RUN_BASE`, físico, owner actual0700 | Sin él ninguna fixture arranca. No usar residuos3821 ni un temporal compartido. |
| Cuota pao-cs propia | `FLEET_TEST_QUOTA_BASE=/Volumes/pao-cs/fleet-tests`, `FLEET_TEST_ISOLATED_QUOTA=1`, `FLEET_TEST_QUOTA_FILL_LIMIT` positivo ≤536870912 | Revalidación futura disk3s7/UUID F4FF2E7E-751F-48B3-BA0D-F5F2656C23BC/Case-sensitive APFS/Owners Enabled/cuota exacta512MiB/cosmonaut0700. Sin espacio previo o con cambio de metadatos ajenos: BLOCKED. La estabilidad no prueba ausencia de uso ajeno. R6 sólo setup; falta discriminante de frontera aprobado, no admite ni llena. |
| Esquemas auténticos | `FLEET_TEST_SCHEMA2_CLI`, `FLEET_TEST_SCHEMA3_CLI`, copias públicas completas de CLI reales | No se siembran tablas/versiones. Inventarios activos sólo con comando en barrera sin escritor antiguo avanzando. |
| Retención edad | `FLEET_TEST_AGED_AUTHORITY`, `FLEET_TEST_AGED_KEY`, autoridad propia e historial auténtico>7d | Sólo consulta; no registra admisión ni limpia/modifica ese estado externo. Falta edad incierta si el historial sólo es finished. No existe clock flag del producto. |
| Negativos OS O03 | Otra cuenta/ejecutor externo y entorno que deniegue realmente identidad nativa | No implementados como sustituto env. Sin ellos los dos negativos permanecen bloqueados. No crear cuentas/permisos globales. |
| Ventanas internas | Control causal externo real, aún no identificado en CLI | Claim/vínculo/R/compuerta/Popen/fsync/commit exactos y B-W1 pre-spawn siguen pendientes; campañas registran sólo lo observado. |

`cleanup` registra todas las admisiones antes de submit; sin stdout usa K. Rechazo
candidato confirmado no se confunde con ausencia de asociación histórica.
Incertidumbre/atribución faltante no autoriza borrar. Se abren gates de fixtures y
se recogen clientes; se exige ausencia observable de supervisores/grupos publicados.
Snapshots, campañas y contadores se guardan con fsync fuera del root eventualmente
retirado. Antes de retirar una fixture completa se conservan también bytes de
manifiestos/payloads públicos, cotejando hash e identidad estable sin atime; se
sincroniza el padre de evidencia. Un error impide retirar originales.
Estados con completion incomplete se retienen, incluso released con evidencia
posteriormente inválida. Una cuota propia fuera del root común se conserva y figura
en owned_bases. C23 conserva snapshots públicos antiguos en el informe; el historial
aged ajeno a la fixture es sólo lectura.

## Conservación de las16 pruebas base

Se mantienen aceptación/identidad; client; argv/cwd/stdin/logs; exit/señal; FIFO y
concurrencia; entorno/PATH por emisor; descendiente en grupo; atribución antes de
limpieza; estado público rechazado sin cambios; timeout/SIGINT del lector;
aislamiento de señales; supervisor perdido; fallo de lanzamiento; dos estados;
errores/log perdido/selección; default/reservados.

Cambios intencionales versionados: JSON en observation y errores tipados; cwd ahora
materializado; fallo de supervisor conserva accepted histórico (método renombrado);
finished/publication pending da3 y helper base sólo reobserva esa ventana; run base
obligatorio, watchdog30s y conservación de evidencia/incompletos. No se elimina un
caso para acomodar un resultado del runtime. R7 no se ha ejecutado; los resultados
históricos de c63 se registran abajo sin reclasificarlos.

## Aclaración r6 y correcciones r7 respecto de c63

El bloqueo inicial del autor se conserva como antecedente. PAO-CLARIFICATION-1
autoriza implementar los tres arreglos y el setup; no autoriza inventar una
observación causal externa. Si se identifica una propuesta admisible, PAO decide
antes de implementarla. No hay nueva observación implementada ni cuota ejecutada.

Lost_supervisor usa una variante local de BARRIER que omite sólo el print; done
es independiente del pipe cuyo lector murió. Sigue SIGKILL al supervisor, wait3,
slot incierto retenido, segundo queued/sin ready y transición única queued. Los
otros usuarios de barrier(), sus variantes de descendientes y entorno/PATH y los
oráculos stdout=ok siguen usando el BARRIER original. No se captura ni acepta
genéricamente un fallo del comando y no se acepta done ausente.

En ambos r4_a01, el helper r4_replay_boundary conserva inventarios completos
antes/después de los lectores a cada lado del replay. Inventory global sigue
incluyendo SHM/WAL, modo/tamaño/mtime/ctime/hash. Sustituye la igualdad física
cruzando el escritor por igualdad contractual CLI: api/operación/autoridad/uid,
revisión, control, operaciones y auditoría completas, seq, recibos, los dos
cursores y fin. Pagina ambos streams limit1 con avance y fin explícitos. Compara
además todas las respuestas status de los holders, incluido el de declaración
posterior, excepto el único observed_at de observación. Esto cubre identidad,
admisión/input, reserva/capacidad, dispatch, transiciones y revisión sin SQL.
Mantiene before/after/control, request/binding exactos, privacidad tras declarar,
literales y las dos historias temporales independientes r5; tampoco elimina los
asserts posteriores de secreto en motivo/ID, conflicto pese a igual redacción,
contador vacío y ausencia de supervisor. No infiere mutación lógica desde SHM.

El setup de cuota inventaría sólo metadatos inmediatos del base y su raíz propia
recursiva. Identifica cada raíz por creación exclusiva/dev/ino/uid0700, sin
atribuir por prefijo; state/project/home/fixtures se preparan dentro. R7 activa
retención administrativa antes del primer quota_record/preflight/mkdtemp: cleanup
conserva self.root y journal original aunque no haya raíz de cuota ni admisiones
o falle esa primera escritura. El snapshot exterior no sustituye los originales.
El registro separa fase real y objetivo, primaria/secundarios, raíz creada y
atribución confirmada. Cada inventario es complete sólo tras enumeración íntegra,
incomplete con entradas parciales y error, o not_performed con motivo. Finally
intenta inventarios permitidos y registro final aun en fallos de configuración,
certificado, espacio, mkdtemp o metadatos. No recorre una raíz sin atribución ni
un base cuya identidad no se confirmó. Conserva rc/stderr de diskutil o error de
consulta/timeout sin publicar el entorno privado. Errores de inventario, persistencia
y cierre quedan explícitos y no sustituyen la misma primaria; sin primaria previa
se propaga el primer fallo real. Journal fuera de cuota: persistence sólo pasa a
synced en memoria tras escritura/fsync de archivo y directorio/cierres completos;
si falla permanece unconfirmed, sin promesa de durabilidad. Se conservan los
originales disponibles incluso si no se puede persistir el diagnóstico final.
No limpia históricos ni genera relleno. Los dos subTest O06
permiten informar ambas fases aunque una precondición falle; siguen64 métodos,
con2 invocaciones subTest condicionales explícitas en O06,0 métodos nuevos.

PASS de cuota requeriría ENOSPC/EDQUOT real en fase objetivo probada, no sólo
llenado, apertura fallida o errno genérico: C20 con ack terminal propio y O06 con
commit de capacidad/auditoría correlacionados. Barreras deben prepararse antes de
llenar y su propio fallo nunca acredita producto. Sólo se podría restituir el
relleno propio atribuido en finally con registro; raíces/estado/evidencia quedan.
Esa experimentación permanece pendiente de una decisión posterior sobre el
discriminante; r6 falla BLOCKED después de setup y jamás produce skip/PASS.

Evidencia histórica c63, run20261026-143952-e9b9eb:3.9=56pass/6fail/2error,
3.14=58pass/4fail/2error,0skip; C20/O06/C23 son3 BLOCKED por stack. C07/C08
timeout5s, C08 además settle10s y63/64 reportes públicos; cierre posterior por
ausencia20:50:26Zrc0 no cambia esas pruebas ni rellena los reportes. Lost_supervisor
no llegó a done; los r4_a01 fallaron inventario sólo3.9. El mecanismo de señales y
BrokenPipe sigue siendo hipótesis histórica delimitada, sin getsignal/EPIPE
medidos de esos clientes. La inclusión/efectos físicos SHM y ventana escritora
sí fueron constatados en fuentes públicas. Son discriminantes históricos de los
tres arreglos; C20/O06 no tuvieron baseline FAIL causal porque no pasaron setup.
R6 conserva64 métodos,0 ejecutados, sin afirmar PASS ni aceptación de plataforma.
C23 permanece BLOCKED; A6/C21 positivo/O10/ventanas/OS mantienen sus límites.

R7 conserva los64 nombres y MRO,0 métodos nuevos/skips y0 ejecutados. Sus tres
discriminantes contra3b son estáticos: fallo antes de quota_roots ya no permite
rmtree del journal; C08 fotografía después de running durable, evitando atribuir
a SIGHUP una transición legítima starting→running; C07 rechaza la respuesta
exterior unknown/unknown que antes satisfacía130/client_interrupted. No hay
FAIL/PASS medidos de esos recorridos. Lost_supervisor y replay r6 quedan intactos;
B-N1/B-N2 y nits anteriores diferidos. Ningún observador causal nuevo ni frontera
C20/O06/C23 cerrada por esa revisión.

R7 mantiene A Astra APTO individual/B Opus NO APTO por W1, sin aceptar ni medir
su frecuencia: entre importtime y la asignación de operation puede responder
unknown. R8 implementa la campaña autorizada sin demoras. Contra af, una respuesta
unknown completa/recogida ya no aborta sólo por identificación temprana: permite
otro intento únicamente con130/client_interrupted/effects unknown oread_only,
imagen pública íntegra sin cambios y contador1. Timeout, falta de recogida/marcador,
JSON inválido, otro retorno/error/efecto o mutación fallan sin reintentar. No lanza
otro cliente si el previo no está recogido. Los10s incluyen la última recogida,
consulta pública y comparaciones, sin renovación por intento; communicate≤5s.
La muestra exige wait/read_only auténtico130 recogido dentro de cota. Conserva
gate final/terminal/wait. Identificación en main no acredita entrada exacta al bucle.

Cada intento guarda antes de los asserts número/PID, UTC de inicio/petición/señal
y fin observado al recoger (no instante interno de exit), stdout y stderr íntegros
con importtime ya consumido/bytes hex, rc, recogida/completitud/errores/faltantes,
duración/remanente, imagen pública y contador. Compara estado/vista/revisión,
transiciones/reserva/resolución/control/dispatch/supervisor/hijo/comando y contador1
con referencia original y anterior. Datos no observados no se rellenan. Registros
en external_evidence para el snapshot de cleanup existente, sin volcar environment
ni prometer persistencia durable si falla escritura/cierre. Agotamiento3/10s:
BLOCKED de preparación del harness, con total de intentos, unknown_count y sus UTC
de señal disponibles/faltantes, nunca PASS/skip/cobertura ni defecto de producto.
No hay frecuencia, FAIL o PASS r8 medidos. R7/r8 no reclasifican c63 ni sus reportes.
Sin demora50ms, sleeps adaptativos/ciegos, hooks ni señales globales/producto.
N1 rótulos r6 no motiva barrido; N2 KeyboardInterrupt secundario de finally de cuota
se declara sin modificar operator ni orden de la primaria. Replace/tamaño/refactor
y nits anteriores siguen diferidos. Retención/inventarios/primaria r7, C08,
lost_supervisor, replay r4 y condicionalidad C20/O06/C23 permanecen intactos.

SEN-5649 3b-2: `test_host_rounds_cli` admite por CLI y planta offline cada forma
válida de `host_rounds`. Lee por `status` concesión/replay, rechazo, retención,
identidades vivas/muertas y los negativos de R1-1, siempre con control positivo.
Un barrido con oráculo explícito altera una columna por vez y exige tanto el
rechazo de incoherencias como la aceptación de alteraciones legítimas. Puede usar
`FLEET_ROUND_SWEEP=view` sólo si su medición CLI excede 120 s por stack.
Esto acredita lectura; escritura/transiciones, despacho host y C21 positivo siguen
abiertos. Los resultados históricos anteriores no cambian.


SEN-5649 3b-3: el adaptador escribe y `status` lee `prepared`, `outcome_unknown`,
`confirmed`, `rejected`, `retired` y `held` con/sin key y recibo. Las pruebas CLI
contra hostgate privado cubren CAS antes del envío, confirmación antes de creación,
ACK perdido con la misma operación, canal cortado y reinicio sin adopción, dos
ledgers con un presupuesto, drenaje tras grant, recibo real alterado, simulación,
cercos del piloto y migración aplazada. Los cortes usan barreras observables,
no demoras para ganar carreras. El arranque permanece cerrado: C21 positivo sigue
abierto, al igual que latido de fondo, resolución de `held` y liberación (PR 4).
Los resultados históricos anteriores no cambian.

SEN-5650 4d-1: ledger v6. `test_host_ledger_v6` planta cada fase con recibos reales de
una autoridad hostgate efectiva en proceso y la lee por `status`: sólo se acepta la
variante de su fase, `held` conserva el prefijo y el permiso, las cinco fases de
creación ocupan el índice de ronda abierta y `released` o `closed_unused` no. También migra v4 y v5 por el
CLI con historia byte a byte idéntica, sin migrar desde un lector, y aplaza la
migración con el slot ocupado. Las transiciones que escriben esas fases, el espejo y
`recover` no están en este corte.

SEN-5650 4d-2: transiciones y espejo. `test_host_creation_mirror` recorre por los CAS de
`storage` authorize, consume, record_started y soltar hasta `released` con el slot
libre en el mismo commit. Además prueba `recover` con el cierre original, con cierres
re-firmados de otro permiso, raíz o línea base, con un intento ya terminado y con una
operación de otro principal con el mismo id o con otro; el grant
sin claim hasta `closed_unused`, sin `queued`→`finished`, también descubierto sin su
clave; el rechazo durable de este principal y no el de otro; un CAS desde un estado
que no es el leído y dos escritores con la misma lectura; `consumed` sin raíz; y la
duda: `held` heredado, `uncertain`, boot nuevo, dueño perdido, tiempo, operador y
`TransportError`. El arranque sigue cerrado: bootstrap, latido y ejecución host son
de 4e. Los resultados históricos anteriores no cambian.
