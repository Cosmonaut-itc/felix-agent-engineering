#!/bin/bash
# Limpieza automática de espacio regenerable. La ejecuta launchd cada hora
# (~/Library/LaunchAgents/com.cosmonaut.limpieza-disco.plist).
# Uso manual: limpieza-disco.sh [--dry-run]
#
# Modo emergencia: si al empezar quedan menos de 15 GB libres, los umbrales de
# antigüedad se acortan (entre paréntesis, el valor en emergencia).
#
# Qué borra:
#   1. ~/remote-run/work/*   sin .remote-run.lock, sin nada modificado en 6 h (2 h)
#                            en sus dos primeros niveles, que no sea el
#                            mirror de una ejecución de las últimas 6 h ni el cwd
#                            de un proceso vivo.
#   2. Temporales de pytest  /private/tmp/bt-*, /private/tmp/gold*, ~/pytest-bt-*
#                            y cualquier carpeta mía de /private/tmp con
#                            subcarpetas popen-gw* (--basetemp de xdist), más los
#                            /private/tmp/wt-* vacíos; con más de 12 h (3 h) sin
#                            tocar, que no sean --basetemp ni cwd de un proceso
#                            vivo ni tengan archivos abiertos.
#   3. ~/remote-run/runs/*   terminadas (tienen archivo exit) hace más de 14 días.
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
  EMERG=1; WORK_MIN=120; PYTEST_MIN=180; CODEX_MIN=1440; CLAUDE_MIN=1440
else
  EMERG=0; WORK_MIN=360; PYTEST_MIN=720; CODEX_MIN=4320; CLAUDE_MIN=4320
fi
log "inicio (libre: ${antes} GB$([ "$EMERG" = 1 ] && echo ", EMERGENCIA")$([ "$DRY" = 1 ] && echo ", dry-run"))"

# Rutas en uso: cwd de todos mis procesos, cualquier --basetemp en línea de
# comandos y todos los archivos que tengo abiertos.
EN_USO=$(mktemp)
{
  lsof -a -u "$(id -u)" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
  ps -axww -o command= | grep -o -- '--basetemp[= ][^ ]*' | sed 's/^--basetemp[= ]//; s/"//g'
  lsof -u "$(id -u)" -Fn 2>/dev/null | sed -n 's/^n\(\/.*\)/\1/p'
} | sed 's#^/tmp/#/private/tmp/#' | sort -u > "$EN_USO"
trap 'rmdir "$LOCK" 2>/dev/null; rm -f "$EN_USO"' EXIT

en_uso() { # ¿algún proceso usa $1 o algo dentro de $1?
  grep -qF -- "$1" "$EN_USO"
}

# 1. remote-run/work
if [ -d "$RR/work" ]; then
  RECIENTES=$(find "$RR/runs" -maxdepth 2 -name meta -mmin -360 -exec grep -h '^mirror=' {} + 2>/dev/null \
    | sed 's#.*/work/##' | sort -u)
  for d in "$RR/work"/*/; do
    d=${d%/}; n=$(basename "$d")
    [ -e "$d/.remote-run.lock" ] && continue
    [ -n "$(find "$d" -maxdepth 2 -mmin -"$WORK_MIN" -print -quit 2>/dev/null)" ] && continue
    printf '%s\n' "$RECIENTES" | grep -qxF -- "$n" && continue
    en_uso "$d" && continue
    [ -e "$d/.remote-run.lock" ] && continue  # re-chequeo justo antes de borrar
    borrar "$d" "remote-run/work"
  done
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

# 3. remote-run/runs terminadas hace más de 14 días
if [ -d "$RR/runs" ]; then
  find "$RR/runs" -mindepth 2 -maxdepth 2 -name exit -mtime +14 2>/dev/null | while read -r f; do
    borrar "$(dirname "$f")" "run terminada >14 d"
  done
fi

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
    -o -path "$HOME/.cargo" \) -prune -o -type d -name .next -print -prune 2>/dev/null |
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
