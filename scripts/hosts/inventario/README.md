# Inventario en seco de remote-run

Python 3.9 o posterior, sólo biblioteca estándar. No escribe, mueve ni borra
archivos. Emite JSON a stdout; `--resumen` selecciona texto legible.

```sh
/usr/bin/python3 -B scripts/hosts/inventario/inventario.py
/usr/bin/python3 -B scripts/hosts/inventario/inventario.py --resumen
/usr/bin/python3 -B scripts/hosts/inventario/inventario.py --raiz /ruta/remote-run --tamanos
```

Las copias necesitan al menos 7 días sin uso. Las corridas terminales y las
preparaciones abortadas necesitan más de 30 días. Los timestamps son epoch en
segundos. `ultimoUso` de una corrida es `started`, o el mtime del directorio
cuando falta. `corridasAsociadas` enumera IDs para copias; en corridas es `[]`.
Se incluyen copias ocultas.

La identidad se acredita con `ps -o lstart= -p PID`, `TZ=UTC0`, `LC_ALL=C`, conversión UTC con meses ingleses propios y timeout de
5 segundos. Una diferencia de hasta 120 segundos con `started` acredita vida.
Un PID ausente o reutilizado acredita terminación. Un fallo de consulta o
registro incompleto protege. `exit` acredita terminación; `phasefailed` no
interviene. Un lock de preparación abortada se protege porque no acredita
una corrida terminal.

Una preparación reconocida contiene únicamente `meta`, `cmd.sh`, `git-input/`,
`git-preparado/`, `manifest/`, `manifest-helper.sh`, `script.sh` y `log`, o está
vacía. Faltan `pid`, `started`, `runner.sh`, `exit` y `exit.tmp`. Se exige listado
sin errores, tipos regulares legibles, directorios reales accesibles y `meta`
válido si existe. Para admitir `log`, `manifest-helper.sh` o `script.sh` hace
falta un `manifest/` accesible con `files.jsonl` regular y legible, la señal del
productor actual. Un log aislado o un runner transferido conserva la duda.
El motivo es «artefactos de preparación; runner no transferido; sin identidad
de ejecución». Su edad usa el mtime; sólo es borrable con más de 30 días.
Un lock que la señale sigue protegido como «preparación reconocida».

Un `meta` mal formado o symlink en un registro lo vuelve dudoso. Si no se
puede asociar una corrida viva o dudosa a una copia, todas las copias que no
tengan evidencia de uso vivo se protegen por dueño desconocido. También se
protegen cuando no se puede listar `runs`. Es una extensión conservadora:
un registro ilegible no debe hacer desaparecer una posible asociación.
Las preparaciones reconocidas no entran en el bloqueo global: acreditan una
etapa anterior al runner. El motivo de bloqueo global indica «ejecución no
descartable». Una duda con `mirror` válido protege sólo su copia y los locks
que la señalen; una viva o dudosa sin dueño recuperable protege globalmente.
El campo `cmd` admite líneas adicionales porque el productor lo escribe
literalmente al final del archivo. No se interpreta como shell.

`--tamanos` recorre sólo candidatos, con scandir y lstat, sin seguir symlinks.
Suma bloques asignados de 512 bytes y cuenta hardlinks una vez por candidato.
Los totales suman candidatos y pueden contar bloques compartidos entre ellos;
no predicen el espacio recuperable de clones APFS. Sin la opción, tamaños y
suma son `null`. Un fallo al recorrer protege ese candidato.

El inventario es una observación, no una autorización de borrado. No es una
instantánea atómica: antes de cualquier borrado futuro se deben repetir las
validaciones de ownership y liveness. No registra atime ni lo restaura; leer
puede actualizar atime según el filesystem, sin modificar archivos ni mtime.

## Pruebas exclusivamente en Mini

Ejecutar por `scripts/mini/run.sh` de comprador con turno. No ejecutar las
pruebas en la MacBook. El guion remoto para este encargo es:

```sh
set -eu
raiz="$PWD"
cd scripts/hosts/inventario
/usr/bin/python3 -B -m unittest discover -s tests -v
cd "$raiz"
/usr/bin/python3 -B scripts/hosts/inventario/inventario.py --resumen
/usr/bin/python3 -B scripts/hosts/inventario/inventario.py > scripts/hosts/inventario/inventario-mini.json
```

El JSON es un artefacto remoto, no un archivo del repo. Recogerlo en la
evidencia con `remote-run pull` a través del mismo `run.sh`, usando
`--reporte scripts/hosts/inventario/inventario-mini.json`. El lanzador prepara
ese reporte y lo recupera tras finalizar. Máximo dos corridas.

## Limpieza automática de la Mini

El LaunchAgent `com.cosmonaut.limpieza-disco` ejecuta cada hora
`/Users/cosmonaut/bin/limpieza-disco.sh`. Borra con `rm -rf` los directorios
no ocultos de `~/remote-run/work/` que cumplen las cuatro condiciones:

- no tienen `.remote-run.lock`;
- no contienen nada modificado en 6 h, o en 2 h en emergencia (menos de 15 GB libres);
- no son la copia de una corrida con `meta` de las últimas 6 h;
- no son el cwd de un proceso vivo del usuario.

También borra corridas de `~/remote-run/runs/` terminadas hace más de 14 días.
Su glob omite directorios ocultos, de modo que sobreviven las copias
`.comprador-*` y las cuarentenas `.borrando-*`. Registra en
`~/Library/Logs/limpieza-disco.log`. Dejarlo, ajustarlo o desactivarlo es decisión
de Felix; el inventario y el borrado acotado no lo tocan.

## Borrado acotado de cuatro copias

`borrar_copias.py` requiere Python 3.9 y sólo biblioteca estándar. La lista
literal autoriza exclusivamente `.comprador-felix-mode-83cc1dfe`,
`.comprador-humo-099-f3371349`, `.comprador-humo-103-680cb5db` y
`.comprador-humo-int-e5fab47b`. Ningún argumento, archivo o variable de entorno
amplía esa lista. El modo predeterminado es **seco**: no renombra ni borra.
Su primera instrucción desactiva el bytecode, antes de cualquier otra
importación, así que el script no escribe cachés de lo que importa. Lo que
carga el arranque del intérprete, antes de esa línea, sólo lo evita `-B`.
Por eso `-B` es obligatorio, y el shebang también lo lleva. El Python 3.9 de
Xcode guarda esas cachés bajo `~/Library/Caches/com.apple.python`, no junto
al código. La invocación es:

```sh
/usr/bin/python3 -B scripts/hosts/inventario/borrar_copias.py
```

`--raiz` permite lecturas sobre fixtures temporales. `--ejecutar` exige que
no se haya pasado `--raiz` y que `/usr/sbin/scutil --get LocalHostName`, con
`LC_ALL=C` y timeout de 10 s, devuelva exactamente `Felixs-Mac-mini`.
La raíz predeterminada se obtiene de `pwd.getpwuid(os.getuid()).pw_dir` más
`/remote-run`, independientemente de `HOME`. El informe seco también registra
el valor leído y si coincide. Las abreviaturas de argumentos se rechazan. **El borrado real queda fuera de
este encargo**: el guion sólo lo ejercita sobre fixtures de sus pruebas.

En ambos modos comprueba soporte de `dir_fd` para rename, open, unlink,
rmdir y stat, y de descriptor en scandir. Abre work una sola vez sin seguir
enlaces, compara su identidad y lo conserva hasta el listado final. Busca
cuarentenas `.borrando-<nombre>-*` de las cuatro copias antes de avanzar. Si
encuentra alguna, registra ruta e identidad y rechaza sin tocarla.

Valida las cuatro copias antes de cualquier escritura: directorios reales por
`lstat`, padre real igual a `raiz/work`, ausencia de cualquier lock e inventario
fresco con `tamanos=True` y `borrable=true`. Recoge `/usr/bin/du -sk` con
argumentos separados y timeout de 30 s junto a `tamanoBytes`. Si cualquier
validación o du falla, no empieza el borrado. El inventario mantiene la
protección de corridas vivas o dudosas y el bloqueo global; `phasefailed`
no acredita terminación (SEN-3284).

Antes de cada copia repite inventario y validación y compara las identidades
`st_dev/st_ino` de raíz, work y copia. Comprueba además raíz/work por ruta
contra las identidades y el descriptor. Antes de renombrar consulta también
`lsof` y `ps` sobre la ruta original (`usoPrevio`); si no acreditan ausencia de
uso, rechaza sin renombrar. Las comprobaciones posteriores siguen mandando.
Renombra sólo por `work_fd` a
`.borrando-<nombre>-<hora UTC>-<secrets.token_hex(8)>`, oculto e impredecible.
Antes de llamar a rename registra `primeraEscritura`, la ruta prevista y el
estado `cuarentena-intentada`. Ese campo indica un intento de escritura, no
que la syscall haya terminado. Tras el rename escribe progreso en stderr con
flush. Abre ese objeto sin seguir enlaces y compara su identidad validada.

Sobre el objeto apartado exige estas cuatro lecturas satisfactorias:

- ausencia de `.remote-run.lock`, con stat sin seguir enlaces relativo al descriptor;
- `/usr/sbin/lsof -a -p ^<PID propio> -nP +D <cuarentena>` con timeout de 120 s:
  libre exclusivamente con exit 1, stdout vacío y stderr vacío; cualquier
  diagnóstico, otro exit o timeout significa error. Antes y después de lsof
  exige que stat de la ruta sin seguir enlaces coincida con fstat del fd.
  Excluye sólo al script porque el
  descriptor abierto para borrar sería detectado como uso propio;
- `/bin/ps -axo pid=,command=` sin líneas que contengan el nombre original o
  el de cuarentena, excluyendo sólo el PID propio; cualquier fallo protege;
- inventario fresco con `tamanos=False`, sin bloqueo global ni corridas vivas
  o dudosas asociadas al nombre original. `proteccionCopias` expone esas
  clasificaciones incluso cuando el nombre ya no existe, sin repetir la lógica
  de liveness en el borrado.

No hay devolución automática ni rename inverso. Cualquier fallo posterior al
rename, por identidad, lock, lsof, ps, inventario o vaciado, conserva el objeto
bajo su nombre oculto con `error-parcial` y detiene la secuencia. El informe
registra nombre original, cuarentena, hora UTC y causa. No toca ninguna copia
siguiente ni una copia nueva que un productor cree con el nombre original.
Recuperarla exige un encargo y una autorización nuevos: se hace a mano con
la evidencia de identidades, nombres y causa. Las cuarentenas previas siguen
rechazando ambos modos.

El inventario aprobado **no reconoce la cuarentena por el prefijo**. La prueba
`test_inventario_clasificacion_real_cuarentena` acredita que un `.borrando-*`
sin locks ni asociaciones se clasifica `reciente` y no borrable con mtime
reciente, o `inactiva` y borrable con al menos siete días. Un rename puede
conservar el mtime anterior. No se clasifica necesariamente como dudosa; es
una desviación de la expectativa de 075. No se cambia `inventario.py`. El
borrado acotado la rechaza por `cuarentenasPrevias` aunque el inventario la
considere borrable. `limpieza-disco` no la toca porque es un directorio oculto.

El inventario puede mostrar una cuarentena `.borrando-*` como
`inactiva`/`borrable=true`; ese «borrable» no autoriza nada: sólo
`borrar_copias` con sus cuatro nombres literales borra, y rechaza si hay
cuarentena.

El vaciado materializa `entradas = list(os.scandir(fd))` antes de borrar;
abre los subdirectorios sin seguir enlaces y rechaza otro `st_dev`. Borra archivos y symlinks con unlink relativo al
padre y directorios vacíos con rmdir relativo. El último paso por nombre es
rmdir de la cuarentena ya vacía. No cambia el cwd. Si falla el vaciado,
registra `error-parcial`, la ruta, el error y un du posterior si puede medirlo.
No restaura ni reintenta y no toca las siguientes copias. Recuperar una copia
en cuarentena o parcialmente borrada requiere un encargo y una autorización
nuevos. Una cuarentena es un directorio oculto y `limpieza-disco` no la toca.

El modelo de amenaza incluye los remote-run de comprador y Bodega, el
LaunchAgent y una persona en una terminal que usa el nombre original.
Tras el rename los productores crean otra copia con rsync. Queda fuera un
proceso del mismo usuario que descubra o adivine la cuarentena y actúe
deliberadamente sobre ella. Las comprobaciones por ruta de raíz/work detectan
su sustitución antes de apartar y al listar; no constituyen una exclusión
contra renombres intencionados posteriores.

**Riesgo residual N2 aceptado por 075, opción B.** El criterio autorizado es
«borrable=true, sin lock ni corridas vivas»; se conserva también la protección
ante corridas dudosas. `lsof` y `ps` son capas extra. No hay observación atómica:
un usuario que ya retiene un fd puede crear y abrir un archivo nuevo, cerrar
su fd de directorio y escapar a la enumeración de `lsof +D`. El peor caso
aceptado es perder un archivo nuevo en un espejo de 7,5–16 días sin corridas,
reconstruible con rsync. No se congelan permisos ni se usa `fchmod 0o500`.
Antes de un borrado real se requiere un seco satisfactorio inmediatamente
antes, en el mismo guion; esta ronda sólo autoriza ejecutar ese seco.

En seco añade lsof y ps sobre cada ruta original, sin cuarentena. Emite JSON
con modo, host, UTC de los pasos, validación, item, du, re-verificación y uso
por copia. En ejecución escribe progreso para cuarentena, borrado y error.
SIGTERM y SIGHUP se convierten en excepciones. Ante una excepción o señal
capturable desde la intención de rename, reconcilia por `work_fd` el nombre
original y el previsto y registra ambas identidades y la ubicación observada.
Un rename que no se consumó deja `rechazada`; uno consumado deja
`error-parcial` en cuarentena. Si el rmdir final se consumó antes de la
interrupción, el fd retenido acredita su desaparición. `main` imprime el
informe parcial con `resultado: "interrumpido"` y vuelve a lanzar la excepción.
No se promete informe ante SIGKILL. Las pruebas de SIGTERM y SIGHUP usan una
barrera después del rename real para evitar depender de temporizaciones.

Los fd de todas las copias apartadas, incluidas las borradas, se mantienen
abiertos hasta terminar el listado final, para evitar la reutilización de sus
inodos. El listado usa el mismo `work_fd` y compara identidad por ruta. Por cada
borrada exige ausencia de su identidad validada y del nombre de cuarentena;
un nombre original con otra identidad se informa `recreada-por-productor` y
se acepta. Exit 0 sólo para `validado-en-seco` y `borrado-completo`; cualquier
rechazo, detención o fallo de listado devuelve 1 (guardas de CLI: 2).

La suite se ejecuta sólo en la Mini con turno de `run.sh`. Sus procesos y
fixtures son temporales y se limpian en la corrida. El guion
`/Users/felixddhs/.claude/encargos/mini-carga/guiones/borrado-mini.sh` imprime
SHA y porcelain (sólo admite `?? .remote-run.lock`), corre ambas suites,
comprueba las mutaciones de las guardas en copias temporales y muestra el
JSON seco del remote-run real con du, lsof y ps de las cuatro. Usa `-B` y no
exporta `PYTHONDONTWRITEBYTECODE`. Una prueba CLI fresca, con
`PYTHONPYCACHEPREFIX` temporal, acredita que el script no escribe cachés
más allá de las que escribe el arranque. `cache_from_source` recibe la ruta
real; el mutante debe caer en esa aserción específica. `run.sh` conserva stdout
en su evidencia.
