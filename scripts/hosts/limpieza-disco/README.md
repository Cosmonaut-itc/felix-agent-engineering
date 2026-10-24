# Limpieza de disco de la Mini

`limpieza-disco.sh` libera espacio regenerable en la Mini. El LaunchAgent
`com.cosmonaut.limpieza-disco` lo ejecuta al cargar y cada hora, con Nice 10 y
E/S de baja prioridad. Escribe en `~/Library/Logs/limpieza-disco.log`. La
cabecera del script lista las ocho reglas y sus umbrales.

## ~/remote-run

Sobre `~/remote-run` sólo actúan las reglas 1 y 3, y las dos dependen de
`~/bin/pao-inventario.py`, la copia instalada de
`scripts/hosts/inventario/inventario.py`.

- **Regla 1.** Borra copias de `work/` que el inventario marca `borrable` con
  clase `inactiva`: siete días sin uso, sin lock y sin corridas vivas o
  dudosas. Antes de borrar, el script comprueba otra vez que la copia no tenga
  `.remote-run.lock` y que ningún proceso la use como cwd ni tenga archivos
  abiertos dentro.
- **Regla 3.** Borra corridas de `runs/` que el inventario marca `borrable`
  con clase `terminal`, más de 30 días, y que tengan archivo `exit`.
- **Entradas ocultas.** Ninguna regla toca un nombre que empiece por punto,
  como `.comprador-*` o `.borrando-*`. Esas entradas sólo las trata
  `borrar_copias.py`, con autorización explícita.
- **Inventario no disponible.** Si el inventario falla, su salida no valida o
  hay un bloqueo global, las reglas 1 y 3 no borran nada en esa pasada y el
  log dice por qué. Hay bloqueo global cuando existen corridas de dueño
  desconocido o cuando no se pueden listar `runs/` o `work/`.
- **Regla 7.** Busca cachés de Next.js en todo el home, pero poda
  `~/remote-run`, así que no entra en las copias.

El script borra un subconjunto del que el inventario llama borrable. Deja las
copias `lock-huerfano` y las corridas `preparacion-abortada`, que el
inventario sí admite.

## Instalar y actualizar

La instalación necesita la autorización del orquestador de PAO. Desde un
checkout de PAO en `main`, sin cambios locales en este directorio ni en
`scripts/hosts/inventario/`:

```sh
bash scripts/hosts/limpieza-disco/instalar.sh --en-seco
bash scripts/hosts/limpieza-disco/instalar.sh
```

- **`--en-seco`.** Copia los archivos a un temporal de la Mini y ejecuta allí
  `limpieza-disco.sh --dry-run` con el inventario del temporal. No cambia
  `~/bin` ni el LaunchAgent.
- **Sin argumentos.** Rechaza la instalación si hay una pasada en curso y
  hace `bootout`. Después respalda `~/bin/limpieza-disco.sh` y el plist con
  el sufijo `.bak-AAAAMMDD`, sin pisar un respaldo existente de ese día.
  Instala el inventario, el script y el plist, y hace `bootstrap`. `RunAtLoad`
  lanza la primera pasada.

El instalador anota en `~/bin/pao-inventario.origen` el SHA de PAO del que
sale `~/bin/pao-inventario.py`. Para actualizar el inventario, vuelve a
ejecutar el instalador desde el `main` nuevo. Todos los `ssh` y `scp`
deshabilitan la multiplexación.

Para volver a la versión anterior, haz `bootout`, copia los `.bak-AAAAMMDD`
sobre el script y el plist, y haz `bootstrap`.
