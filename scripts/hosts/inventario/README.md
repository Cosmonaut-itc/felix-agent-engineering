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
Desactiva el bytecode antes de importar el inventario. La invocación es:

```sh
/usr/bin/python3 -B scripts/hosts/inventario/borrar_copias.py
```

`--raiz` permite lecturas sobre fixtures temporales. `--ejecutar` exige que
no se haya pasado `--raiz` y que `/usr/sbin/scutil --get LocalHostName`, con
`LC_ALL=C` y timeout de 10 s, devuelva exactamente `Felixs-Mac-mini`.
El informe seco también registra el valor leído y si coincide. Las
abreviaturas de argumentos se rechazan. **El borrado real queda fuera de
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
contra las identidades y el descriptor. Renombra sólo por `work_fd` a
`.borrando-<nombre>-<hora UTC>-<secrets.token_hex(8)>`, oculto e impredecible.
Registra inmediatamente su ruta en el informe y en stderr con flush. Abre
ese objeto una sola vez sin seguir enlaces y compara su identidad validada.

Sobre el objeto apartado exige estas cuatro lecturas satisfactorias:

- ausencia de `.remote-run.lock`, con stat sin seguir enlaces relativo al descriptor;
- `/usr/sbin/lsof -a -p ^<PID propio> -nP +D <cuarentena>` con timeout de 120 s:
  libre exclusivamente con exit 1 y stdout vacío; cualquier línea significa uso,
  y otro exit o timeout significa error. Excluye sólo al script porque el
  descriptor abierto para borrar sería detectado como uso propio;
- `/bin/ps -axo pid=,command=` sin líneas que contengan el nombre original o
  el de cuarentena, excluyendo sólo el PID propio; cualquier fallo protege;
- inventario fresco con `tamanos=False`, sin bloqueo global ni corridas vivas
  o dudosas asociadas al nombre original. `proteccionCopias` expone esas
  clasificaciones incluso cuando el nombre ya no existe, sin repetir la lógica
  de liveness en el borrado.

Cualquier rechazo devuelve la copia mediante rename por `work_fd` sólo si
el nombre original está ausente. La devolución queda registrada con hora UTC.
Si un productor recreó el nombre, conserva ambos objetos, informa
`en-cuarentena` y se detiene. Una identidad distinta tras apartar también
provoca devolución y detención, sin vaciar el objeto.

El vaciado usa `scandir(fd)`, abre los subdirectorios sin seguir enlaces y
rechaza otro `st_dev`. Borra archivos y symlinks con unlink relativo al
padre y directorios vacíos con rmdir relativo. El último paso por nombre es
rmdir de la cuarentena ya vacía. No cambia el cwd. Si falla el vaciado,
registra `error-parcial`, la ruta, el error y un du posterior si puede medirlo.
No restaura ni reintenta y no toca las siguientes copias. Recuperar una copia
en cuarentena o parcialmente borrada requiere un encargo y una autorización
nuevos. Una cuarentena es un directorio oculto y `limpieza-disco` no la toca.

El modelo de amenaza incluye los remote-run de comprador y Bodega, el
LaunchAgent y una persona en una terminal que usa el nombre original.
Tras el renombre los productores crean otra copia con rsync; quienes ya
conservan cwd o archivos abiertos forman un conjunto cerrado que lsof
inspecciona. Queda fuera un proceso del mismo usuario que descubra o adivine
la cuarentena y actúe deliberadamente sobre ella. Las comprobaciones por
ruta de raíz/work detectan su sustitución antes de apartar y al listar; no
constituyen una exclusión contra renombres intencionados posteriores.

En seco añade lsof y ps sobre cada ruta original, sin cuarentena. Emite JSON
con modo, host, UTC de los pasos, validación, item, du, re-verificación y uso
por copia. En ejecución escribe progreso para cuarentena, devolución,
borrado y error. Ante BaseException después de una escritura, imprime el
informe parcial con `resultado: "interrumpido"` y vuelve a lanzar la excepción.

El listado final usa el mismo `work_fd` y compara identidad por ruta. Por cada
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
exporta `PYTHONDONTWRITEBYTECODE`; una prueba CLI fresca acredita que no se
crean cachés por efecto del script. `run.sh` conserva stdout en su evidencia.
