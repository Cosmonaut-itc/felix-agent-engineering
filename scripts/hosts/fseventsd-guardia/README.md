# Guardia de fseventsd

El 2026-11-16 la Mini acumuló 61 GB de footprint (58 GB comprimidos) y
52 GB de swap: `fseventsd` llevaba 7 días al 100 % de CPU. Tras reiniciarlo,
con la misma carga usó 3,5 MB y 0,2 % de CPU. El RSS ocultaba la fuga.

LaunchDaemon root cada 10 minutos. Lee la segunda muestra de `top` (footprint,
no RSS). Reinicia con `killall fseventsd` si alcanza 8 GiB o 18 muestras
consecutivas con CPU ≥ 90 %, con enfriamiento de 60 minutos. Flags:
`--footprint-gib`, `--cpu`, `--consecutivas`, `--enfriamiento-min`.
Estado atómico en `/Library/Application Support/pao/fseventsd-guardia/estado.json`.
Historial en `/Library/Logs/fseventsd-guardia.log`, incluido el pid nuevo.
Si una lectura falla, registra el error sin actuar.

`./instalar.sh` **imprime** los comandos sudo exactos para ejecutar manualmente
en una terminal de la Mini. No ejecuta sudo ni pide o guarda contraseñas.
Los comandos contienen los bytes de HEAD publicado en base64, comprueban sus
SHA-256, guardan SHA/blob/sumas en `origen`, instalan como root:wheel y recargan
`system` con bootout/bootstrap. El script que ejecuta root queda en
`/Library/Application Support/pao/fseventsd-guardia/guardia.py`, modo 755,
nunca en un home editable por el usuario. Conserva los temporales locales.

`./instalar.sh --en-seco` ejecuta por SSH los bytes publicados en la Mini con
`--dry-run`: mide y decide sin matar ni escribir estado. No requiere sudo.
Python 3.9 sin dependencias. Pruebas (en host remoto):
`python3 -I -m unittest discover -s scripts/hosts/fseventsd-guardia/tests -v`.
