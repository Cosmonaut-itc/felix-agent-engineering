# Inventario en seco de remote-run

Python 3.9 o posterior, sólo biblioteca estándar. No escribe, mueve ni borra
archivos. Emite JSON a stdout; `--resumen` selecciona texto legible.

```sh
/usr/bin/python3 scripts/hosts/inventario/inventario.py
/usr/bin/python3 scripts/hosts/inventario/inventario.py --resumen
/usr/bin/python3 scripts/hosts/inventario/inventario.py --raiz /ruta/remote-run --tamanos
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
export PYTHONDONTWRITEBYTECODE=1
raiz="$PWD"
cd scripts/hosts/inventario
/usr/bin/python3 -m unittest discover -s tests -v
cd "$raiz"
/usr/bin/python3 scripts/hosts/inventario/inventario.py --resumen
/usr/bin/python3 scripts/hosts/inventario/inventario.py > scripts/hosts/inventario/inventario-mini.json
```

El JSON es un artefacto remoto, no un archivo del repo. Recogerlo en la
evidencia con `remote-run pull` a través del mismo `run.sh`, usando
`--reporte scripts/hosts/inventario/inventario-mini.json`. El lanzador prepara
ese reporte y lo recupera tras finalizar. Máximo dos corridas.

## Borrado acotado de cuatro copias

`borrar_copias.py` requiere Python 3.9 y sólo biblioteca estándar. Su lista
literal autoriza exclusivamente `.comprador-felix-mode-83cc1dfe`,
`.comprador-humo-099-f3371349`, `.comprador-humo-103-680cb5db` y
`.comprador-humo-int-e5fab47b`. Ningún argumento, archivo o variable de entorno
permite ampliar esa lista. `--raiz` cambia la raíz, por defecto `~/remote-run`,
para admitir fixtures temporales.

El modo predeterminado es **seco**: valida e informa sin borrar nada. El
argumento `--ejecutar` habilita el borrado, que queda **fuera de este encargo**.
El guion y las corridas reales de esta implementación sólo usan el modo seco:

```sh
/usr/bin/python3 scripts/hosts/inventario/borrar_copias.py
```

Antes de avanzar exige las cuatro copias reales mediante `lstat`, padre real
igual a `raiz/work`, ausencia de cualquier `.remote-run.lock` (incluso
huérfano) e inventario fresco con `tamanos=True` y `borrable=true`. El
inventario conserva la protección por corridas vivas o dudosas asociadas y
el bloqueo global por dueño desconocido o `runs` ilegible. `phasefailed`
no acredita terminación: la lección de SEN-3284 sigue vigente.

Recoge `/usr/bin/du -sk` para las cuatro, con argumentos separados y timeout
de 30 segundos, junto con `tamanoBytes`. Una validación previa o un `du`
fallidos impiden todo borrado. Después repite el inventario completo antes
de cada copia, incluso en seco. Una re-verificación fallida detiene esa copia
y las siguientes; una ejecución real podría haber borrado las anteriores.

Exige `shutil.rmtree.avoids_symlink_attacks` verdadero en ambos modos. En
ejecución, vuelve a comprobar las identidades de raíz, work y copia, abre
work sin seguir enlaces y ancla allí el directorio actual durante `rmtree`,
restaurándolo al terminar. Así no vuelve a atravesar ancestros reemplazados.
Los enlaces internos se eliminan como enlaces, sin recorrer su destino.
Las observaciones de locks y procesos siguen sin ser una instantánea atómica
ni una reserva frente a productores concurrentes.

Emite un JSON a stdout con modo, horas UTC de inicio y fin de los pasos,
totales del inventario inicial, validación e item completo por nombre, `du`,
re-verificación y resultado. Los pasos no alcanzados quedan en `null` y los
resultados pendientes en `no-procesada`. Siempre intenta listar work al final
y comprueba mediante `lstat` que las copias borradas ya no existen; en seco
comprueba que las conservadas siguen presentes. Exit 0 sólo para
`validado-en-seco` o `borrado-completo`; rechazo, detención o fallo de listado
devuelven 1.

Las pruebas nuevas usan exclusivamente raíces temporales: el borrado y sus
fallos se ejercitan allí. La suite completa se corre sólo en la Mini con
`run.sh` y turno, nunca en la MacBook. El guion de este encargo es
`/Users/felixddhs/.claude/encargos/mini-carga/guiones/borrado-mini.sh`:
imprime y valida el porcelain (sólo tolera `?? .remote-run.lock`), ejecuta
ambas suites y muestra el JSON del modo seco contra el remote-run real.
No crea un reporte dentro de la copia; `run.sh` conserva stdout en su evidencia.
