#!/bin/bash
# Limpieza automática de espacio regenerable. La ejecuta launchd cada hora
# (~/Library/LaunchAgents/com.cosmonaut.limpieza-disco.plist).
# Uso manual: limpieza-disco.sh [--dry-run]
#
# Modo emergencia: si al empezar quedan menos de 15 GB libres, los umbrales de
# antigüedad se acortan (entre paréntesis, el valor en emergencia).
#
# Qué borra:
#   1. ~/remote-run/work/*   sólo las copias no ocultas que ~/bin/pao-inventario.py
#                            marca borrable (inactiva: 7 días sin uso, sin lock,
#                            sin corridas vivas o dudosas ni bloqueo global), que
#                            sigan sin .remote-run.lock y sin procesos dentro
#                            según un lsof hecho justo antes de borrar. Nunca
#                            toca entradas ocultas
#                            (.comprador-*, .borrando-*): ésas sólo las trata
#                            borrar_copias.py con autorización explícita.
#   2. Temporales de pytest  /private/tmp/bt-*, /private/tmp/gold*, ~/pytest-bt-*
#                            y cualquier carpeta mía de /private/tmp con
#                            subcarpetas popen-gw* (--basetemp de xdist), más los
#                            /private/tmp/wt-* vacíos; con más de 12 h (3 h) sin
#                            tocar, que no sean --basetemp ni cwd de un proceso
#                            vivo ni tengan archivos abiertos.
#   3. ~/remote-run/runs/*   sólo las que el inventario clasifica terminal y
#                            borrable (más de 30 días), no ocultas, con exit y
#                            sin procesos dentro. Corre justo después de la 1.
#      Si el inventario falla, su salida no se valida, hay bloqueo global o no
#      puede descartarse, las reglas 1 y 3 no borran nada en esa pasada y el log
#      dice por qué. Borran desde el directorio padre fijado como cwd.
#      Ninguna otra regla entra en ~/remote-run.
#   4. Caché de Bun          bun pm cache rm, como mucho una vez por semana y sólo
#                            si pasa de 10 GB.
#   5. Worktrees de Codex    ~/.codex/worktrees/* sin tocar en 3 días (1 día), sin
#                            cambios locales, con HEAD ya en el remoto y sin
#                            procesos dentro (git worktree remove); también los
#                            vacíos.
#   6. Scratchpads de Claude /private/tmp/claude-UID/-proyecto/sesión sin ningún
#                            archivo modificado en 3 días (1 día) ni procesos dentro.
#   7. Cachés de Next.js     .next/cache y .next/dev bajo el home (Turbopack escribe
#                            ahí GB por sesión de next dev). Se borran si nadie los
#                            tiene abiertos, nada cambió en 30 min, ningún proceso
#                            tiene su app como cwd, y además pesan más de 1 GB o
#                            llevan 1 día sin tocarse (en emergencia, siempre).
#   8. Simuladores de Xcode  xcrun simctl delete unavailable, una vez por semana.

set -u
export PATH="$HOME/.bun/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

RR="$HOME/remote-run"
STATE="$HOME/Library/Application Support/limpieza-disco"
LOCK="$STATE/lock"
mkdir -p "$STATE"

log() { echo "$(date '+%F %T') $*"; }
libre_gb() { df -g /System/Volumes/Data | awk 'NR==2{print $4}'; }

borrar() { # $1 = ruta, $2 = motivo
  if [ "$DRY" = 1 ]; then
    log "[dry-run] borraría $1 ($2)"
  else
    rm -rf -- "$1" && log "borrado $1 ($2)"
  fi
}

borrar_en() { # $1 = directorio padre sin symlinks, $2 = nombre, $3 = motivo
  # Con el padre como cwd, sustituirlo después por un symlink no desvía el rm.
  ( cd -P -- "$1" 2>/dev/null && [ "$(pwd -P)" = "$1" ] && [ -d "$2" ] && [ ! -L "$2" ] || exit 3
    [ "$DRY" = 1 ] && exit 0
    rm -rf -- "./$2" )
  case $? in
    0) log "$([ "$DRY" = 1 ] && echo '[dry-run] borraría' || echo borrado) $1/$2 ($3)" ;;
    3) log "no borro $1/$2 ($3): el padre no es $1 o la entrada cambió" ;;
  esac
}

# Evita dos ejecuciones simultáneas; un lock de más de 2 h se considera huérfano.
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    rmdir "$LOCK" && mkdir "$LOCK" || exit 0
  else
    log "otra limpieza en curso; salgo"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

antes=$(libre_gb)

# Umbrales en minutos: normales y en emergencia (<15 GB libres).
if [ "$antes" -lt 15 ]; then
  EMERG=1; PYTEST_MIN=180; CODEX_MIN=1440; CLAUDE_MIN=1440
else
  EMERG=0; PYTEST_MIN=720; CODEX_MIN=4320; CLAUDE_MIN=4320
fi
log "inicio (libre: ${antes} GB$([ "$EMERG" = 1 ] && echo ", EMERGENCIA")$([ "$DRY" = 1 ] && echo ", dry-run"))"

# Reglas 1 y 3: el inventario decide qué es borrable en ~/remote-run.
# Instalado junto a este script; desde un temporal permite una pasada en seco previa.
INVENTARIO="$(cd "$(dirname "$0")" && pwd)/pao-inventario.py"
INV_JSON=$(mktemp); INV_LISTA=$(mktemp); INV_ERR=$(mktemp)
trap 'rmdir "$LOCK" 2>/dev/null; rm -f "$INV_JSON" "$INV_LISTA" "$INV_ERR"' EXIT
INV_OK=0
if [ ! -d "$RR" ]; then
  INV_MOTIVO="no existe $RR"
elif ! /usr/bin/python3 -B "$INVENTARIO" --raiz "$RR" > "$INV_JSON" 2> "$INV_ERR"; then
  INV_MOTIVO="el inventario falló: $(tr '\n' ' ' < "$INV_ERR" | cut -c1-300)"
elif ! /usr/bin/python3 -B - "$INV_JSON" "$RR" > "$INV_LISTA" 2> "$INV_ERR" <<'PY'
import json
import os
import sys

datos = json.load(open(sys.argv[1]))
raiz = os.path.abspath(sys.argv[2])
if datos.get('schema') != 'remote-run-inventario@1' or os.path.abspath(datos.get('raiz', '')) != raiz:
    sys.exit('esquema o raíz inesperados')
work, runs = os.path.join(raiz, 'work'), os.path.join(raiz, 'runs')
bloqueos = [c['motivo'] for c in datos['copias']
            if c['motivo'].startswith('ejecución no descartable; corridas de dueño desconocido')]
bloqueos += [c['motivo'] for c in datos['corridas'] if os.path.abspath(c['ruta']) == runs]
bloqueos += [c['motivo'] for c in datos['copias'] if os.path.abspath(c['ruta']) == work]
if bloqueos:
    sys.exit('bloqueo global del inventario: ' + bloqueos[0])
# El inventario sólo clasifica una copia como inactiva, reciente o lock-huerfano
# cuando no halló corridas de dueño desconocido; sin una así, el bloqueo no se descarta.
if not any(c['clase'] in ('inactiva', 'reciente', 'lock-huerfano') for c in datos['copias']):
    sys.exit('sin prueba de ausencia de bloqueo global: ninguna copia llegó a clasificarse')


def nombre(ruta, padre):
    base = os.path.basename(ruta)
    if (os.path.dirname(os.path.abspath(ruta)) != padre or not base
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in base)):
        sys.exit('ruta inesperada en el inventario: ' + ruta)
    return base


def visible(c):
    return not os.path.basename(c['ruta']).startswith('.')


for c in datos['copias']:
    if c['borrable'] is True and c['clase'] == 'inactiva' and visible(c):
        print('work\t' + nombre(c['ruta'], work))
for c in datos['corridas']:
    if c['borrable'] is True and c['clase'] == 'terminal' and visible(c):
        print('runs\t' + nombre(c['ruta'], runs))
PY
then
  INV_MOTIVO="salida del inventario no válida: $(tr '\n' ' ' < "$INV_ERR" | cut -c1-300)"
else
  INV_OK=1
fi
[ "$INV_OK" = 1 ] || log "reglas 1 y 3 sin borrar: $INV_MOTIVO"

# Rutas en uso: cwd de todos mis procesos, cualquier --basetemp en línea de
# comandos y todos los archivos que tengo abiertos.
EN_USO=$(mktemp)
{
  lsof -a -u "$(id -u)" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
  ps -axww -o command= | grep -o -- '--basetemp[= ][^ ]*' | sed 's/^--basetemp[= ]//; s/"//g'
  lsof -u "$(id -u)" -Fn 2>/dev/null | sed -n 's/^n\(\/.*\)/\1/p'
} | sed 's#^/tmp/#/private/tmp/#' | sort -u > "$EN_USO"
trap 'rmdir "$LOCK" 2>/dev/null; rm -f "$EN_USO" "$INV_JSON" "$INV_LISTA" "$INV_ERR"' EXIT

en_uso() { # ¿algún proceso usa $1 o algo dentro de $1?
  grep -qF -- "$1" "$EN_USO"
}

libre_ahora() { # sin lock y sin procesos dentro, con un lsof hecho justo antes de borrar
  if [ -e "$1/.remote-run.lock" ] || [ -L "$1/.remote-run.lock" ]; then return 1; fi
  local salida estado
  salida=$(lsof -nP +D "$1" 2>&1); estado=$?
  [ "$estado" = 1 ] && [ -z "$salida" ]
}

# 1. remote-run/work: copias no ocultas que el inventario marca borrables
if [ "$INV_OK" = 1 ]; then
  while IFS=$'\t' read -r tipo n; do
    [ "$tipo" = work ] || continue
    case "$n" in ""|.*|*/*) continue ;; esac
    d="$RR/work/$n"
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    en_uso "$d" && continue
    libre_ahora "$d" || { log "no borro $d: lock o proceso dentro"; continue; }
    borrar_en "$RR/work" "$n" "inventario: inactiva y borrable"
  done < "$INV_LISTA"
fi

# 3. remote-run/runs: terminales que el inventario marca borrables (más de 30 días).
# Va justo tras la 1 para usar el mismo inventario recién tomado.
if [ "$INV_OK" = 1 ]; then
  while IFS=$'\t' read -r tipo n; do
    [ "$tipo" = runs ] || continue
    case "$n" in ""|.*|*/*) continue ;; esac
    r="$RR/runs/$n"
    [ -d "$r" ] && [ ! -L "$r" ] && [ -e "$r/exit" ] || continue
    en_uso "$r" && continue
    libre_ahora "$r" || { log "no borro $r: proceso dentro"; continue; }
    borrar_en "$RR/runs" "$n" "inventario: run terminal >30 d"
  done < "$INV_LISTA"
fi

# 2. Temporales de pytest
for d in /private/tmp/* "$HOME"/pytest-bt-*; do
  [ -d "$d" ] && [ ! -L "$d" ] && [ -O "$d" ] || continue
  case "$d" in
    /private/tmp/bt-*|/private/tmp/gold*|"$HOME"/pytest-bt-*) motivo="temporal de pytest" ;;
    *)
      if [ -n "$(find "$d" -mindepth 1 -maxdepth 1 -type d -name 'popen-gw*' -print -quit 2>/dev/null)" ]; then
        motivo="basetemp de pytest-xdist"
      elif [[ "$d" == /private/tmp/wt-* ]] && [ -z "$(ls -A "$d" 2>/dev/null | grep -vx '.DS_Store')" ]; then
        motivo="carpeta wt-* vacía"
      else
        continue
      fi ;;
  esac
  [ -n "$(find "$d" -maxdepth 3 -mmin -"$PYTEST_MIN" -print -quit 2>/dev/null)" ] && continue
  en_uso "$d" && continue
  borrar "$d" "$motivo"
done

# 4. Caché de Bun: semanal y sólo si pasa de 10 GB
BUN_CACHE="$HOME/.bun/install/cache"
STAMP="$STATE/bun-cache-stamp"
if [ -d "$BUN_CACHE" ] && { [ ! -e "$STAMP" ] || [ -n "$(find "$STAMP" -mtime +6)" ]; }; then
  kb=$(du -skx "$BUN_CACHE" 2>/dev/null | cut -f1)
  if [ "${kb:-0}" -gt $((10 * 1024 * 1024)) ]; then
    if [ "$DRY" = 1 ]; then
      log "[dry-run] vaciaría la caché de Bun ($((kb / 1048576)) GB)"
    else
      bun pm cache rm >/dev/null 2>&1 && log "caché de Bun vaciada ($((kb / 1048576)) GB)"
      touch "$STAMP"
    fi
  fi
fi

# 5. Worktrees de Codex con más de 3 días, limpios, con HEAD en el remoto y sin procesos dentro
for wt in "$HOME"/.codex/worktrees/*/; do
  wt=${wt%/}
  [ -n "$(find "$wt" -maxdepth 0 -mmin -"$CODEX_MIN")" ] && continue
  en_uso "$wt" && continue
  repo=$(ls -d "$wt"/*/.git 2>/dev/null | head -1); repo=${repo%/.git}
  if [ -z "$repo" ]; then
    [ -z "$(ls -A "$wt" | grep -vx '.DS_Store')" ] && borrar "$wt" "worktree de Codex vacío"
    continue
  fi
  [ -z "$(git -C "$repo" status --porcelain 2>/dev/null | grep -v '^?? .codex-artifacts/')" ] || continue
  [ -n "$(git -C "$repo" branch -r --contains HEAD 2>/dev/null)" ] || continue
  if [ "$DRY" = 1 ]; then
    log "[dry-run] quitaría el worktree de Codex $wt"
  else
    if git -C "$repo" worktree remove --force "$repo"; then
      rm -f "$wt/.DS_Store"; rmdir "$wt" 2>/dev/null
      log "quitado el worktree de Codex $wt"
    else
      log "no pude quitar el worktree de Codex $wt"
    fi
  fi
done

# 6. Scratchpads de sesiones de Claude sin actividad en 3 días
for s in /private/tmp/claude-"$(id -u)"/-*/*/; do
  s=${s%/}
  [ -d "$s" ] && [ -O "$s" ] || continue
  [ -n "$(find "$s" -mmin -"$CLAUDE_MIN" -print -quit 2>/dev/null)" ] && continue
  en_uso "$s" && continue
  borrar "$s" "scratchpad de Claude inactivo"
done

# 7. Cachés de Next.js (.next/cache y .next/dev) de apps sin servidor vivo
find "$HOME" -xdev \( -path "$HOME/Library" -o -path "$HOME/.Trash" -o -name node_modules -o -name .git \
    -o -path "$HOME/.cache" -o -path "$HOME/.bun" -o -path "$HOME/.rustup" -o -path "$HOME/.android" \
    -o -path "$HOME/.cargo" -o -path "$HOME/remote-run" \) -prune -o -type d -name .next -print -prune 2>/dev/null |
while read -r nx; do
  app=$(dirname "$nx")
  # Un proceso con cwd en la app (p. ej. next dev) la protege entera.
  grep -qxF -- "$app" "$EN_USO" && continue
  for c in "$nx/cache" "$nx/dev"; do
    [ -d "$c" ] || continue
    en_uso "$c" && continue
    [ -n "$(find "$c" -mmin -30 -print -quit 2>/dev/null)" ] && continue
    kb=$(du -skx "$c" 2>/dev/null | cut -f1)
    if [ "$EMERG" = 0 ] && [ "${kb:-0}" -le $((1024 * 1024)) ] \
       && [ -n "$(find "$c" -mmin -1440 -print -quit 2>/dev/null)" ]; then
      continue
    fi
    borrar "$c" "caché de Next.js, $((kb / 1024)) MB"
  done
done

# 8. Simuladores de Xcode no disponibles: semanal
STAMP_SIM="$STATE/simctl-stamp"
if command -v xcrun >/dev/null && { [ ! -e "$STAMP_SIM" ] || [ -n "$(find "$STAMP_SIM" -mtime +6)" ]; }; then
  if [ "$DRY" = 1 ]; then
    log "[dry-run] ejecutaría xcrun simctl delete unavailable"
  else
    xcrun simctl delete unavailable >/dev/null 2>&1 && log "simuladores no disponibles borrados"
    touch "$STAMP_SIM"
  fi
fi

log "fin (libre: $(libre_gb) GB, antes: ${antes} GB)"
