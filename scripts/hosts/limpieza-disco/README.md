# Limpieza de disco de la Mini

`limpieza-disco.sh` libera espacio regenerable en la Mini. El LaunchAgent
`com.cosmonaut.limpieza-disco` lo ejecuta al cargar y cada hora, con Nice 10 y
E/S de baja prioridad. Escribe en `~/Library/Logs/limpieza-disco.log`. La
cabecera del script lista las ocho reglas y sus umbrales.

## Recorridos y exclusión mutua

Cada `find`, `du` y `lsof` del shell tiene un tope de 600 segundos, sin
coreutils. También lo tienen el inventario, su medición recursiva de una
copia y la captura adicional de cwd. Diez minutos dan margen sobre la
pasada normal de 1–3 minutos y cortan la espera indefinida de File Provider
o NFS. El supervisor Python inicia un grupo de procesos separado y, al
vencer el plazo, envía SIGKILL al grupo, registra regla y ruta y devuelve
124. Espera como máximo un segundo adicional: un proceso retenido por el
kernel puede tardar en morir. Sus salidas van a archivos temporales, de
modo que ese proceso tampoco puede retener un pipe y bloquear la pasada.

Sólo se publica stdout tras completar el recorrido con éxito. La excepción
es `lsof` con código 1: conserva su salida para que una captura parcial no
se confunda con ausencia de uso. Un error o corte de actividad conserva la
candidata; uno de tamaño tampoco autoriza borrado; uno de inventario o
corridas nuevas cierra las reglas 1 y 3. Un corte del listado de cachés no
publica candidatas. El log de topes usa un descriptor separado para seguir
visible aunque la consulta capture stdout y stderr.

El único recorrido que parte del home (regla 7) poda Documents y Desktop,
que en la Mini tienen `com.apple.file-provider-domain-id`, y OrbStack, que
está montado por NFS. Library ya estaba podado y cubre CloudStorage y Mobile
Documents. Se fijan las rutas conocidas: no hace falta consultar proveedores
durante la limpieza. `-xdev` sigue presente, pero por sí solo no excluye
iCloud en el mismo volumen. No se siguen enlaces simbólicos.

El lock registra pid, inicio del proceso y comando. Pasados 120 minutos
sólo se recupera si el dueño murió, cambió su identidad o dejó de ejecutar
la limpieza. Un dueño vivo o una consulta no verificable conserva el lock y
queda registrado. Para locks legados sin dueño se comprueba que no haya
otra limpieza viva. La adquisición y recuperación se serializan con
`flock` de Python sobre `lock.guard`; el sistema suelta ese cerrojo si muere
el recuperador. El archivo puede permanecer sin bloquear. No hay un límite
global de pasada: muchas candidatas pueden sumar más de 120 minutos; en
ese caso una invocación posterior registra el dueño vivo y sale.

## ~/remote-run

Sobre `~/remote-run` sólo actúan las reglas 1 y 3. Las dos dependen de
`~/bin/pao-inventario.py`, la copia instalada de
`scripts/hosts/inventario/inventario.py`. El script ejecuta el inventario al
empezar la pasada y aplica las reglas 1 y 3 justo después, antes de las
demás.

- **Regla 1.** Borra copias de `work/` que el inventario marca `borrable` con
  clase `inactiva`: siete días sin uso, sin lock y sin corridas vivas o
  dudosas.
- **EMERGENCIA work.** Sólo si al iniciar quedan menos de 40 GB libres,
  la regla 1 usa seis horas sin uso. El shell pasa explícitamente
  `--copia-minutos 360` al inventario y recorre el árbol completo con
  `find -mmin -360`. Excluye de esa comprobación `.git/index`,
  `.git/FETCH_HEAD`, `.git/ORIG_HEAD`, los archivos `.git/*.lock` del primer
  nivel y el mtime
  del directorio `.git` (el reemplazo de `index` también lo modifica).
  Sigue recorriendo los demás hijos de `.git`: por ejemplo, un `HEAD`
  reciente veta la copia. Las exclusiones exigen archivos regulares y que
  `.git` sea directorio para ignorar su mtime: un enlace reciente se conserva.
  El nombre de la copia se escapa como literal en los patrones de `find`;
  sus metacaracteres no amplían las exclusiones a directorios anidados.
  Un cambio reciente fuera de `.git`, incluso
  profundo, también la veta. El limpiador legado de la Mini ejecuta
  `git status --porcelain` periódicamente sobre copias viejas: ese comando
  puede refrescar el índice sin usar la copia para una corrida.
  Repite el mtime directo de la copia con el mismo umbral. En modo normal pasa
  `--copia-minutos 10080` y mantiene `find -mtime -7`.
  Ordena las candidatas por `ultimoUso`, de más vieja a más nueva, y mide
  espacio antes y después de cada copia. Para la regla 1 cuando `df` alcanza
  60 GB reales. Los tamaños estimados sólo se registran: no frenan.
  No hay otro tope: si no se alcanza el objetivo, agota las candidatas que
  superan todas las protecciones. En la primera pasada de la Mini,
  23.125 GB contabilizados sólo produjeron 3 GB observados; un límite
  basado en esa suma interrumpiría la emergencia prematuramente.
  Conserva bloqueo global, locks, corridas vivas/dudosas, preparación reciente,
  revalidación de corridas nuevas y rename exclusivo.
  La protección de cwd cubre **los procesos del usuario que ejecuta la
  limpieza**, que crea y usa `~/remote-run`. Toma sus PID vivos con
  `ps -U <uid> -o pid=,stat=` antes de consultar una sola vez
  `lsof -a -d cwd -Fn`. Valida la captura entera antes de la primera candidata:
  cada registro debe ser `p<pid>`, `fcwd`, `n<ruta absoluta>`, sin PID repetidos,
  campos sueltos ni rutas vacías. Si un PID propio falta, vuelve a consultar
  `ps`: si sigue vivo y falta, no borra ninguna copia. Excluye zombies, que ya
  no tienen cwd. Error, diagnóstico, captura vacía o formato no verificable
  también cierran la regla 1 con motivo en el log.
  Compara rutas físicas (`realpath`) con frontera de directorio: un cwd igual
  o dentro de una copia la protege. Los procesos de root y de otros usuarios
  quedan fuera de esta cobertura; lo mitigan los permisos: en emergencia
  exige que `~/remote-run` y `~/remote-run/work` sean directorios regulares
  sin bits de lectura ni ejecución para group u other (`mode & 0055 == 0`).
  Si no cumplen, no borra copias ni cambia sus permisos. Esos permisos no
  impiden el acceso de root. La captura es una observación; no elimina la
  ventana de un proceso que cambie su cwd después.
  Mide sólo el tamaño de la copia candidata con el lector del inventario,
  sin cambiar la clasificación de `runs/`. El log anota nombre, bytes asignados
  y último uso UTC por copia, y totales.
  `--dry-run` hace las mismas comprobaciones sin rename ni borrado y enumera
  todas las candidatas mientras `df` siga por debajo de 60 GB. La suma
  registrada no predice cuántas borraría una pasada real: APFS puede
  compartir bloques o retenerlos en snapshots. El log distingue
  los bytes de las copias y el cambio de libre observado por `df`.
  Las demás reglas y la emergencia de menos de 15 GB conservan sus umbrales.
- **Regla 3.** Borra corridas de `runs/` que el inventario marca `borrable`
  con clase `terminal`, más de 30 días, y que tengan archivo `exit`.
- **Orden de comprobación de una copia.** Desde el padre fijado con `cd -P`
  y verificado con `pwd -P`, primero recorre los dos primeros niveles con
  `find -maxdepth 2 -mtime -7` y comprueba las corridas de `runs/` posteriores
  a `MARCA`, creada antes del inventario. Una corrida nueva sin `meta`, con
  lectura fallida, formato no admitido por `espejo()` del inventario o
  `mirror` de la misma copia (sin distinguir mayúsculas) veta la copia.
  Después consulta `EN_USO` y ejecuta `lsof +D`. Exige salida vacía, además
  de código 1; cualquier diagnóstico o estado distinto conserva la copia.
  Al final vuelve a comprobar el lock, con `-e` y `-L`, y exige mediante
  `stat` directo que el mtime del propio directorio tenga al menos siete
  días. No hay otro recorrido de árbol entre ese bloque y la acción.
- **Preparación en curso.** Si el inventario ve una corrida
  `preparacion-abortada` de menos de 1 h, la regla 1 no borra nada en esa
  pasada. Esa corrida todavía no tiene `mirror` en `meta`, así que no se sabe
  qué copia usa.
- **Rename antes de borrar.** La regla 1 comprueba con `stat -f %d` que
  `~/remote-run` y `$STATE` (`~/Library/Application Support/limpieza-disco`)
  están en el mismo filesystem. Desde el padre fijado renombra la copia a
  `$STATE/borrando-<nombre>-<UTC>-<aleatorio>`, con aleatorio de
  `openssl rand -hex 8`. Usa `renamex_np` con `RENAME_EXCL`: un destino
  existente o un cambio de filesystem hacen fallar el rename, sin copiar ni
  sobrescribir. Sólo tras el rename ejecuta `rm -rf` sobre el nombre nuevo.
  Un fallo de rename conserva la copia y se anota; un fallo de `rm` deja el
  resto en `$STATE` y se anota. Al empezar, cada `borrando-*` previo se
  registra y se conserva: sólo se trata con autorización explícita.
  En `--dry-run` se comprueba la candidata sin renombrar ni borrar.
- **Borrado anclado.** Las dos reglas fijan su padre físico como cwd. La
  regla 3 conserva el borrado de `./<nombre>` sin rename y exige `exit` de
  nuevo antes de actuar. Si alguien sustituye el padre por un symlink
  después de entrar, la ruta relativa permanece anclada al padre fijado.
- **Entradas ocultas.** Ninguna regla toca un nombre que empiece por punto,
  como `.comprador-*` o `.borrando-*`. Esas entradas sólo las trata
  `borrar_copias.py`, con autorización explícita.
- **Nombres raros.** Un nombre con caracteres de control, tabulador incluido,
  invalida la salida del inventario, y la pasada no borra nada en
  `~/remote-run`.
- **Bloqueo global.** Las reglas 1 y 3 no borran nada en estos casos, y el
  log dice por qué:
  - el inventario falla;
  - su salida no valida;
  - no se pueden listar `runs/` o `work/`;
  - una copia trae el motivo de corridas de dueño desconocido;
  - ninguna copia llegó a clasificarse como `inactiva`, `reciente` o
    `lock-huerfano`. Sólo esas clases prueban que el inventario no halló
    corridas de dueño desconocido, porque su JSON no publica esa lista.
- **Fallos de comprobación.** Se comprueban todos los `mktemp`, la
  existencia de `MARCA`, el estado de cada `find` crítico, las lecturas de
  `meta`, la captura y lectura de `EN_USO`, `lsof`, `cd -P`, `pwd -P` y el
  inventario. Un fallo conserva la candidata y deja motivo en el log.
  Si no se puede comprobar `runs/` en conjunto (incluida la pérdida de
  `MARCA`), se detienen ambas reglas para el resto de la pasada. Un fallo
  de un temporal exclusivo del inventario (`MARCA`, `INV_JSON`, `INV_LISTA`
  o `RUNS_NUEVAS`) o del listado de restos `borrando-*` detiene sólo las
  reglas 1 y 3. Si falla un temporal compartido (`INV_ERR`, `EN_USO` o
  `USO_*`), se detienen también las reglas 2, 5, 6 y 7, que consultan
  `EN_USO`. Cada fallo deja su motivo en el log y ningún veto se revierte
  durante la pasada.
  Si la lectura inicial de `df` no es un entero no negativo, toda la pasada
  sale con código 1 antes de cualquier regla, también fuera de emergencia.
  Es un cierre deliberado ante espacio libre no verificable.
- **Captura de uso.** Los filtros de cwd, basetemp y archivos abiertos se
  ejecutan por separado y añaden a `EN_USO` todas las rutas recuperables.
  Se acumulan los errores de las fuentes (`id`, ambos `lsof` y `ps`), de la
  extracción de basetemp y de cada `sed`, `sort` o escritura de `EN_USO`;
  cada causa se registra. Un fallo al abrir la salida de basetemp cuenta
  como fallo de extracción y no como ausencia de coincidencias. Cualquier
  fallo, o diagnóstico de `lsof` o `ps`, deja sin borrar las reglas 1, 2, 3,
  5, 6 y 7 durante esa pasada. Las reglas 4 y 8, que no consultan `EN_USO`,
  siguen.
- **Regla 7.** Busca cachés de Next.js en el home con las exclusiones
  de iCloud y NFS descritas arriba, y poda
  `~/remote-run` y `~/.local/state`, así que no entra en las copias, el
  registro de propietarios de Fleet ni el estado de comprador.

El script borra un subconjunto del que el inventario llama borrable. Deja las
copias `lock-huerfano` y las corridas `preparacion-abortada`, que el
inventario sí admite.

Las pruebas de `tests/test_emergencia_work.py` extraen sólo la regla 1 a un
harness temporal con HOME de fixture. Cubren `--dry-run` y borrado real sólo
dentro del fixture, con rename exclusivo y freno sólo por `df`.
Simulan `df` y, en los casos de decenas de GB, los tamaños estimados,
reemplazando las funciones únicamente en esa copia temporal: el script de
producción no admite variables ni flags para falsear el espacio. Los casos
cwd lanzan un `sleep` real dentro del fixture. Para comparar las mismas
pruebas contra main, `LIMPIEZA_TEST_SOURCE` puede señalar un `git archive`
temporal de `origin/main`; sólo el harness interpreta esa variable.
Ejecutar las suites exclusivamente en la Mini por
`REMOTE_RUN_HOST=mini remote-run start --name limpieza-emergencia`.
`tests/test_recorridos_acotados.py` ejecuta la pasada completa con HOME y
temporales de fixture, comandos falsos en PATH y un tope de 0,5 segundos
inyectados sólo en la copia temporal. Comprueba cortes de find/du/lsof,
salidas parciales, continuidad hasta `fin`, podas y recuperación del lock.
También admite `LIMPIEZA_TEST_SOURCE` para comparar contra main.

**Premisa y ventanas residuales.** El productor `remote-run` sincroniza
con `rsync --delete` sin tomar el lock de la copia. El lock lo escribe
`runner.sh` después del rsync, de la preparación de git y del scp de `meta`.
La captura del inventario y el trabajo sobre candidatas anteriores pueden
durar minutos; por eso cada copia repite sus comprobaciones en el orden
indicado antes de actuar.

- **Ventana A:** del fin del rsync a `mkdir runs/<id>`, sin lock, sin proceso,
  sin corrida nueva y con el mtime que el rsync restaura del origen. El
  limpiador no puede deducir esa preparación. Puede borrar la copia antes
  de que el productor la use y provocar un fallo de preparación o de entrada
  al directorio. SEN-3915 cambia el productor para tomar el lock antes del
  rsync: es el cierre de esta ventana y, hasta que se mergee e instale, la
  ventana sigue abierta.
- **Ventana B:** una corrida que arranca durante el `rm -rf` podría operar
  sobre un árbol a medias y producir un resultado incorrecto sin fallo
  explícito. El rename previo mitiga esta ventana: el nombre original queda
  ausente y una corrida nueva lo recrea limpio, mientras `rm` actúa sobre el
  nombre separado en `$STATE`. Sigue sin haber exclusión compartida entre
  la comprobación final y el rename; un proceso que ya hubiera abierto la
  copia puede conservar referencias al árbol renombrado.

Felix acepta estas ventanas con la opción C.

## Instalar y actualizar

La instalación necesita la autorización del orquestador de PAO. El commit de
partida debe estar publicado en un remoto. Los archivos se extraen de ese
commit con `git show`, no del árbol de trabajo.

```sh
bash scripts/hosts/limpieza-disco/instalar.sh --en-seco
bash scripts/hosts/limpieza-disco/instalar.sh
```

- **`--en-seco`.** Copia los archivos a un temporal de la Mini, comprueba sus
  sumas y ejecuta allí `limpieza-disco.sh --dry-run` con el inventario del
  temporal. No cambia `~/bin` ni el LaunchAgent. Usa el lock y el directorio
  de estado reales, así que sale sin evaluar si hay una pasada en curso, y
  sólo recupera un lock de más de 2 h si descarta que su dueño siga vivo,
  como haría la pasada horaria. Un lock legado con limpieza viva también
  impide evaluar: el dry-run lo informa y sale.
- **Sin argumentos.** Hace lo siguiente, en orden:
  1. Comprueba las sumas.
  2. Toma el lock de la limpieza; si está ocupado, aborta.
  3. Solicita `bootout`, tolera su error y espera hasta 10 s a que el job
     deje de estar cargado; si sigue cargado, sale con error.
  4. Respalda `~/bin/limpieza-disco.sh` como `~/bin/limpieza-disco.sh.bak-AAAAMMDD`.
     El plist se respalda en `~/Library/Application Support/limpieza-disco/`, fuera de
     `LaunchAgents`. Ninguno pisa un respaldo existente de ese día.
  5. Instala el inventario, el script y el plist.
  6. Suelta el lock y hace `bootstrap`. `RunAtLoad` lanza la primera pasada.

  Si algo falla después del `bootout`, intenta recargar el plist que haya en
  sitio y avisa si el LaunchAgent queda descargado.

El instalador anota en `~/bin/pao-inventario.origen` el SHA de PAO, si está
en `origin/main`, y el blob y el sha256 de `~/bin/pao-inventario.py`. Avisa
cuando el commit no está en `origin/main`. Para actualizar el inventario, vuelve
a ejecutar el instalador desde el `main` nuevo. Todos los `ssh` y `scp`
deshabilitan la multiplexación.

Para volver a la versión anterior, haz `bootout`, copia los `.bak-AAAAMMDD`
sobre el script y el plist, y haz `bootstrap`.
