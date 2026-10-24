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
- **Comprobación previa.** Antes de borrar cada candidata, el script
  comprueba que no tenga `.remote-run.lock`, ni como archivo ni como
  symlink. También exige que `lsof +D` salga con código 1 y sin salida; si
  no, anota en el log el código y la primera línea de la salida.
- **Última comprobación de una copia.** Ya dentro del padre, justo antes del
  `rm`, vuelve a mirar tres cosas sobre la copia:
  - que siga sin lock;
  - que nada en sus dos primeros niveles haya cambiado en los últimos 7
    días;
  - que en `runs/` no haya una corrida posterior al inicio del inventario
    sin `meta` o con `mirror=remote-run/work/<nombre>`.

  Si falla alguna, la deja y lo anota.
- **Preparación en curso.** Si el inventario ve una corrida
  `preparacion-abortada` de menos de 1 h, la regla 1 no borra nada en esa
  pasada. Esa corrida todavía no tiene `mirror` en `meta`, así que no se sabe
  qué copia usa.
- **Borrado anclado.** El borrado entra con `cd -P` en `work/` o `runs/`,
  comprueba que `pwd -P` sea la ruta esperada y borra `./<nombre>`. Si
  alguien sustituye el padre por un symlink después de entrar, `rm` no lo
  sigue.
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
- **Regla 7.** Busca cachés de Next.js en todo el home, pero poda
  `~/remote-run`, así que no entra en las copias.

El script borra un subconjunto del que el inventario llama borrable. Deja las
copias `lock-huerfano` y las corridas `preparacion-abortada`, que el
inventario sí admite.

**Riesgo residual.** La pasada puede durar minutos, porque `lsof +D` y el
`rm -rf` de las candidatas anteriores tardan. Ese tramo lo cubre la última
comprobación de cada copia. Queda sin cubrir la ventana entre esa última
comprobación y el `rm`. Una corrida que entre y salga en ella sin dejar lock,
sin tocar el mtime y sin proceso vivo no se detecta. No hay un protocolo de
exclusión compartido con `remote-run` que la cierre. El orquestador de PAO
aceptó ese riesgo para copias con siete días sin uso, porque `remote-run`
escribe el lock al preparar y toca el árbol al correr.

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
  3. Hace `bootout`.
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
