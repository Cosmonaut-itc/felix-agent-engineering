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
argv/contenido. Cada literal se separa por `:` antes de comprobar identidad,
también si el origen aparece en una posición no inicial. No se modifica argv.
Primero se busca el texto de la raíz; después se comprueban cantidad, longitud
y profundidad de todos los candidatos antes de resolver ninguno físicamente.
Se admiten hasta 40.000 candidatos por valor, 16.384 bytes por ruta y 128 barras
por ruta, incluida la ruta completa de un argumento absoluto. Una lista puede
contar de nuevo sus componentes al inspeccionar también sus tokens. Los ancestros
se recorren de forma perezosa, sin materializar una tupla de prefijos. El límite
de 16 KiB conserva la fixture binaria anterior de 8 KiB; es una cota de trabajo,
no una afirmación de PATH_MAX del host. Una ruta/candidato que excede una cota
rechaza el envío con `source_reference_limit`, incluso dentro de un binario;
no se considera segura por no poder resolverla. Los 32 MiB por archivo son un
máximo adicional, no una promesa de admitir todo binario de ese tamaño. La comprobación
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

EACCES, EPERM o ELOOP al obtener identidad física producen
`path_identity_unavailable`, sin publicar la ruta ni admitir un alias que no
pueda contrastarse. Es una pérdida explícita de disponibilidad: una referencia
externa inaccesible o cíclica también puede rechazar una entrada ordinaria.
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

`test_cli.py` conserva las 68 conductas públicas anteriores y añade once de
`review_r2_cases.py`: privados por identidad, listas literales, límites de coste,
identidad inaccesible y actores de relevo entre emisores. Son 79 casos públicos
y el caso focalizado de ciclo de vida conservado en `test_ledger_lifecycle.py`:
80 escritos, cero ejecutados. Las tres fixtures de coste calibran primero una
cota efectiva de memoria de 256 MiB en procesos propios; si no pueden acreditarla
(incluido Darwin), fallan antes de lanzar el escáner. El control previo reserva
como máximo 320 MiB de payload, más el intérprete, en un proceso ya acotado por
la propia fixture. Su ejecución requiere presupuesto Mini para ese pico previo.
No ejecutar el rojo de coste sin seguir el procedimiento de COVERAGE.
Los positivos de objetos distintos pueden usar una base temporal propia
`FLEET_CASE_SENSITIVE_TMPDIR` en volumen sensible, con el resto de aliases en la
base habitual case-insensitive; no se crea ni monta ningún volumen desde la suite.
La matriz, selector rojo/verde y fronteras sin fixture están en
[`tests/COVERAGE.md`](tests/COVERAGE.md).
Los casos públicos usan subprocess del CLI, fixtures y recursos propios; no
importan Fleet ni consultan SQL. La prueba focalizada importa el Ledger real
en un subprocess propio y usa primitivas externas para retener un lector más
de diez segundos antes de liberar. No reproduce públicamente la finalización
nativa durante copia BD/WAL. El estado incompatible usa una copia cerrada propia y
el campo público `user_version` del header SQLite, sin tablas ni SQL. Incluyen
snapshots sin Git, Git/índice/working tree,
privacidad positiva y negativa, entorno/PATH en cola, streams simultáneos y un
fallo de escritura mediante RLIMIT_FSIZE real, artefactos inválidos y pérdida
posterior, concurrencia inicial, lecturas sin efectos y rechazos de entradas.
El límite público ejercita escritura; no afirma inyectar separadamente un fallo
de cierre/fsync. El soporte concreto de flock y durabilidad sigue pendiente.

Cada admisión se inventaría antes de lanzar, incluidos wrappers/directos y
concurrencia. Timeout, JSON ilegible o respuesta perdida conservan el pendiente.
Sólo se refuta ausencia con cliente terminado y BD ausente antes/después en el
mismo estado exclusivo, sin borrado intermedio. La limpieza exige atribución y
ausencia efectiva de procesos/grupos propios mediante señal 0; ante duda falla
y retiene temporales. Barreras con autoexit, deadlines acotados y ningún proceso
ajeno señalado. No hay instalaciones ni CI nuevo.

El watchdog de 30 segundos de la matriz de 144 consultas se conserva: su autoexit
da error y `done=watchdog`, nunca liberación sana. Su duración y margen siguen
pendientes de medición en Mini. No se han aumentado timeouts para ocultar un rojo.

Las pruebas nuevas están **escritas, no ejecutadas**. Este candidato requiere
evidencia Mini, reviews Astra high y Opus high, veto PAO y OK concreto de Felix;
no declara el ticket aprobado ni cerrado.
