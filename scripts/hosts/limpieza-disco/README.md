# Limpieza de disco de la Mini

`limpieza-disco.sh` libera espacio regenerable en la Mini. El LaunchAgent
`com.cosmonaut.limpieza-disco` lo ejecuta al cargar y cada hora, con Nice 10 y
E/S de baja prioridad. Escribe en `~/Library/Logs/limpieza-disco.log`. La
cabecera del script lista las ocho reglas y sus umbrales.

## ~/remote-run

Sobre `~/remote-run` sólo actúan las reglas 1 y 3. Las dos dependen de
`~/bin/pao-inventario.py`, la copia instalada de
`scripts/hosts/inventario/inventario.py`. El script ejecuta el inventario al
empezar la pasada y aplica las reglas 1 y 3 justo después, antes de las
demás.

- **Regla 1.** Borra copias de `work/` que el inventario marca `borrable` con
  clase `inactiva`: siete días sin uso, sin lock y sin corridas vivas o
  dudosas.
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
  de temporales o de captura de `EN_USO` termina la pasada sin borrar.
- **Regla 7.** Busca cachés de Next.js en todo el home, pero poda
  `~/remote-run`, así que no entra en las copias.

El script borra un subconjunto del que el inventario llama borrable. Deja las
copias `lock-huerfano` y las corridas `preparacion-abortada`, que el
inventario sí admite.

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
  recupera un lock de más de 2 h, como haría la pasada horaria.
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
