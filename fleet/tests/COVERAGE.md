# SEN-3821, corrección integral r3: cobertura propuesta

Sólo inspección estática. Ninguna prueba se ha ejecutado en este encargo.
La suite pública es `test_cli.py`, que incorpora cuatro mixins. Se conservan las
57 conductas anteriores: 16 heredadas, 21 de contenido, 18 de corrección r1 y
dos de temporales r2. La corrección r3 añade once casos públicos y un caso
focalizado de ciclo de vida: 69 escritos, cero ejecutados. En la suite pública
no se importa Fleet ni se consulta SQL. Cada submit se registra antes de lanzar;
los rechazos usan estado exclusivo, cliente terminado y BD ausente antes/después.
Un fallo de atribución o de cierre propio conserva los temporales.

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
  pública. Usa sólo sus nombres, sin inventariar HOME. Si esos valores aparecen
  ya en la ruta privada de la fixture, declara skip para respetar el rechazo de
  credenciales en paths; ese skip deja pendiente la cobertura correspondiente.
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
  salida 0 y retirada de WAL/SHM. Usa pipes/flock externos, sin mocks, hooks de
  producto ni timeout global modificado. Finalmente libera, recoge al hijo y
  sólo borra su raíz tras confirmar su salida; conserva recursos ante duda.
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

Procedimiento rojo/verde r3, **no ejecutado**: preparar en Mini dos copias propias
separadas, roja desde cb8f812 y verde desde el head exacto entregado, con ambos
worktrees originales y FIFO129 intactos. `INTEGRAL_CANDIDATE` contiene el SHA
completo entregado e `INTEGRAL_RED_COPY` la ruta de la copia roja. Aplicar sólo el
delta del harness a la copia roja, desde la copia candidata:

```sh
git diff cb8f812ac40aa62de68a8503e6853ed750960775 "$INTEGRAL_CANDIDATE" -- \
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
reales sin atribuirles aquí ejecución. Después correr las 68 conductas públicas
y la focalizada completas. Una ejecución con skips de aliases no cierra B1.

## Temporales del lector: regresiones r2 pendientes de Mini

`reader_temp_cases.py` usa exclusivamente subprocess del CLI público, sin importar
Fleet, editar el ledger, ejecutar SQL ni sustituir funciones. Cada consulta es
un proceso nuevo, sin caché heredada de `tempfile`. Se retiran las tres variables
temporales y se introduce sólo la variable bajo prueba: TMPDIR, TEMP o TMP.
Cada una apunta al estado, a un subdirectorio privado suyo, a un symlink al estado
o a una cadena de symlinks al subdirectorio. Para cada combinación se consultan
status/wait/evidence con el job real y con un identificador desconocido.

Hay 72 consultas por fixture. La quieta se obtiene con submit/wait y cierre
propio acreditado por el harness; no tiene WAL/SHM. La activa usa una carga
sin escrituras al estado tras alcanzar running y una barrera FIFO externa:
sólo el controlador libera al productor después de toda la matriz. Un watchdog
de 30 segundos termina con error 91, nunca con éxito; el marcador externo done
debe seguir ausente después de cada lector y decir released al finalizar.
No se amplían los deadlines heredados ni se usa el autoexit de seis segundos
para sostener la comparación. El finally libera incluso ante fallo y el harness
conserva sus comprobaciones de atribución y cierre antes de borrar recursos.
La fixture exige WAL con contenido y SHM presente, y observa running desde
la versión efectiva que incluye WAL. Tras liberar y cerrar exige ambos ausentes.

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

Selector rojo/verde, **no ejecutado aquí**. El coordinador debe preparar en Mini
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
Ambas expectativas necesitan ejecución, también la suite completa de 68 casos CLI
en los dos Python previstos; los casos antiguos no sustituyen esta matriz.

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
  Tras cierre propio/checkpoint se exige ausencia de ambos sidecars y lecturas
  exitosas sin recrearlos. Estado inexistente, vacío, inseguro e incompatible se
  conserva sin bootstrap/reparación. El incompatible se obtiene de una BD propia
  cerrada: se cambia a 999 el entero big-endian público de `user_version`, offset
  60 del header SQLite; no se abren tablas ni se usa SQL.
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
