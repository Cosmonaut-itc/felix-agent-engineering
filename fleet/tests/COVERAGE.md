# SEN-3821, corrección r1: cobertura propuesta

Sólo inspección estática. Ninguna prueba se ha ejecutado en este encargo.
La suite pública es `test_cli.py`, que incorpora los dos mixins. Las 16 conductas
heredadas y los 21 casos de contenido se conservan; la corrección añade 18 casos.
No se importa Fleet ni se consulta SQL. Cada submit se registra antes de lanzar;
los rechazos usan estado exclusivo, cliente terminado y BD ausente antes/después.
Un fallo de atribución o de cierre propio conserva los temporales.

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
