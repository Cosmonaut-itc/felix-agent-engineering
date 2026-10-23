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

La identidad se acredita con `ps -o lstart= -p PID`, locale C y timeout de
5 segundos. Una diferencia de hasta 120 segundos con `started` acredita vida.
Un PID ausente o reutilizado acredita terminación. Un fallo de consulta o
registro incompleto protege. `exit` acredita terminación; `phasefailed` no
interviene. Un lock de preparación abortada se protege porque no acredita
una corrida terminal.

Un `meta` mal formado o symlink en un registro lo vuelve dudoso. Si no se
puede asociar una corrida viva o dudosa a una copia, todas las copias que no
tengan evidencia de uso vivo se protegen por dueño desconocido. También se
protegen cuando no se puede listar `runs`. Es una extensión conservadora:
un registro ilegible no debe hacer desaparecer una posible asociación.
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
