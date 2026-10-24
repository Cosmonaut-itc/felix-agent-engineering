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
#                            según un lsof hecho justo antes de renombrar a
#                            STATE/borrando-* y borrar allí. Nunca
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
log() { echo "$(date '+%F %T') $*"; }
mkdir -p "$STATE" || { log "sin borrar: no pude crear STATE ($STATE)"; exit 1; }
libre_gb() { df -g /System/Volumes/Data | awk 'NR==2{print $4}'; }

borrar() { # $1 = ruta, $2 = motivo
  if [ "$DRY" = 1 ]; then
    log "[dry-run] borraría $1 ($2)"
  else
    rm -rf -- "$1" && log "borrado $1 ($2)"
  fi
}

corrida_nueva() {
  local motivo estado
  if [ ! -f "$MARCA" ] || [ -L "$MARCA" ]; then
    log "reglas 1 y 3 sin borrar: MARCA no existe o no es regular"; return 2
  fi
  if [ ! -d "$RR/runs" ] || [ -L "$RR/runs" ]; then
    log "reglas 1 y 3 sin borrar: runs/ no es un directorio regular"; return 2
  fi
  if ! find "$RR/runs" -mindepth 1 -maxdepth 1 -newer "$MARCA" -print0 > "$RUNS_NUEVAS" 2> "$INV_ERR"; then
    log "reglas 1 y 3 sin borrar: falló find de corridas nuevas en runs/"; return 2
  fi
  if [ ! -f "$MARCA" ] || [ -L "$MARCA" ]; then
    log "reglas 1 y 3 sin borrar: MARCA desapareció durante find"; return 2
  fi
  if [ ! -d "$RR/runs" ] || [ -L "$RR/runs" ]; then
    log "reglas 1 y 3 sin borrar: runs/ cambió durante find"; return 2
  fi
  # Reutiliza lector y formato del inventario, incluido cmd multilínea.
  motivo=$(/usr/bin/python3 -B - "$INVENTARIO" "$RUNS_NUEVAS" "$1" 2>&1 <<'PY'
from pathlib import Path
import os
import runpy
import sys

try:
    inventario = runpy.run_path(sys.argv[1])
    with open(sys.argv[2], 'rb') as archivo:
        listado = archivo.read()
    if listado and not listado.endswith(b'\0'):
        raise ValueError('listado de corridas nuevas incompleto')
except Exception as exc:
    print('no pude comprobar el listado de runs/ o cargar el inventario: ' + str(exc), file=sys.stderr)
    sys.exit(2)
for entrada in listado.split(b'\0')[:-1]:
    ruta = Path(os.fsdecode(entrada))
    try:
        mirror = inventario['espejo'](inventario['leer'](ruta / 'meta'))
    except (OSError, ValueError, UnicodeError) as exc:
        sys.exit('lectura o formato de meta no válido en {}: {}'.format(ruta, exc))
    if mirror is None:
        sys.exit('corrida nueva sin meta en ' + str(ruta))
    if mirror.casefold() == sys.argv[3].casefold():
        sys.exit('corrida nueva asociada a la copia en ' + str(ruta))
PY
  )
  estado=$?
  if [ "$estado" = 2 ]; then
    log "reglas 1 y 3 sin borrar: ${motivo%%$'\n'*}"; return 2
  fi
  if [ "$estado" != 0 ]; then
    log "no borro $RR/work/$1: comprobación de meta falló o vetó la copia: ${motivo%%$'\n'*}"
    return 0
  fi
  return 1
}

borrar_en() {
  local estado
  # El cwd fija el padre aunque después sustituyan su ruta por un symlink.
  (
    cd -P -- "$1" 2>/dev/null || { log "no borro $1/$2: falló cd -P del padre"; exit 1; }
    padre=$(pwd -P) || { log "no borro $1/$2: falló pwd -P del padre"; exit 1; }
    [ "$padre" = "$1" ] && [ -d "./$2" ] && [ ! -L "./$2" ] || {
      log "no borro $1/$2: el padre no es $1 o la entrada cambió"; exit 1;
    }
    if [ "${4:-}" = copia ]; then
      reciente=$(find "./$2" -maxdepth 2 -mtime -7 -print -quit 2> "$INV_ERR") || {
        log "no borro $1/$2: falló find de actividad de la copia"; exit 1;
      }
      [ -z "$reciente" ] || { log "no borro $1/$2: algo cambió en los últimos 7 días"; exit 1; }
      corrida_nueva "$2"; estado_corridas=$?
      [ "$estado_corridas" = 2 ] && exit 2
      [ "$estado_corridas" = 1 ] || exit 1
      dev_rr=$(stat -f %d "$RR") && dev_state=$(stat -f %d "$STATE") || {
        log "no borro $1/$2: falló stat del filesystem de remote-run o STATE"; exit 1;
      }
      [[ "$dev_rr" =~ ^[0-9]+$ ]] && [ "$dev_rr" = "$dev_state" ] || {
        log "no borro $1/$2: remote-run y STATE no acreditan el mismo filesystem"; exit 1;
      }
      aleatorio=$(openssl rand -hex 8) && [[ "$aleatorio" =~ ^[0-9a-f]{16}$ ]] || {
        log "no borro $1/$2: no pude generar el aleatorio del rename"; exit 1;
      }
      utc=$(date -u +%Y%m%dT%H%M%SZ) && [ -n "$utc" ] || {
        log "no borro $1/$2: no pude fechar el rename en UTC"; exit 1;
      }
      destino="$STATE/borrando-$2-$utc-$aleatorio"
    fi
    en_uso "$1/$2" && { log "no borro $1/$2: aparece en EN_USO o no pudo consultarse"; exit 1; }
    libre_ahora "./$2" || exit 1
    if [ "${4:-}" = copia ]; then
      # No hay recorridos entre estas comprobaciones finales y el rename.
      if [ -e "./$2/.remote-run.lock" ] || [ -L "./$2/.remote-run.lock" ]; then
        log "no borro $1/$2: apareció un lock"; exit 1
      fi
      mtime=$(stat -f %m "./$2") && ahora=$(date +%s) || {
        log "no borro $1/$2: falló stat del mtime directo o date"; exit 1;
      }
      [[ "$mtime" =~ ^[0-9]+$ ]] && [[ "$ahora" =~ ^[0-9]+$ ]] || {
        log "no borro $1/$2: mtime directo o fecha no válidos"; exit 1;
      }
      (( ahora - mtime >= 7 * 86400 )) || {
        log "no borro $1/$2: el mtime directo tiene menos de 7 días"; exit 1;
      }
      if [ "$DRY" = 1 ]; then
        log "[dry-run] borraría $1/$2 ($3; rename previo a $destino)"; exit 0
      fi
      # renamex_np con RENAME_EXCL no copia entre volúmenes ni pisa un destino.
      if ! /usr/bin/python3 -B - "./$2" "$destino" <<'PY'
import ctypes
import os
import sys

libc = ctypes.CDLL(None, use_errno=True)
rename = libc.renamex_np
rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
rename.restype = ctypes.c_int
RENAME_EXCL = 0x00000004
if rename(os.fsencode(sys.argv[1]), os.fsencode(sys.argv[2]), RENAME_EXCL) != 0:
    codigo = ctypes.get_errno()
    sys.exit('rename exclusivo falló: ' + os.strerror(codigo))
PY
      then
        log "no borro $1/$2: falló el rename exclusivo a $destino"; exit 1
      fi
      log "renombrada $1/$2 a $destino"
      if rm -rf -- "$destino"; then
        log "borrado $destino ($3)"
      else
        log "falló rm de $destino: queda en STATE; requiere autorización explícita"; exit 1
      fi
    else
      [ -e "./$2/exit" ] || { log "no borro $1/$2: ya no tiene exit"; exit 1; }
      if [ "$DRY" = 1 ]; then
        log "[dry-run] borraría $1/$2 ($3)"
      elif rm -rf -- "./$2"; then
        log "borrado $1/$2 ($3)"
      else
        log "no pude borrar $1/$2 ($3)"; exit 1
      fi
    fi
  )
  estado=$?
  [ "$estado" = 2 ] && INV_OK=0
  return "$estado"
}

# Evita dos ejecuciones simultáneas; un lock de más de 2 h se considera huérfano.
if ! mkdir "$LOCK" 2>/dev/null; then
  lock_viejo=$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null) || {
    log "sin borrar: falló find de antigüedad del lock de limpieza"; exit 1;
  }
  if [ -n "$lock_viejo" ]; then
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
MARCA= INV_JSON= INV_LISTA= INV_ERR= EN_USO= USO_CWD= USO_ABIERTOS= USO_PS= USO_BASETEMP= RUNS_NUEVAS=
limpiar_temporales() {
  rmdir "$LOCK" 2>/dev/null
  for temporal in "$MARCA" "$INV_JSON" "$INV_LISTA" "$INV_ERR" "$EN_USO" \
      "$USO_CWD" "$USO_ABIERTOS" "$USO_PS" "$USO_BASETEMP" "$RUNS_NUEVAS"; do
    [ -z "$temporal" ] || rm -f -- "$temporal"
  done
}
trap limpiar_temporales EXIT
for variable in MARCA INV_JSON INV_LISTA INV_ERR EN_USO USO_CWD USO_ABIERTOS USO_PS USO_BASETEMP RUNS_NUEVAS; do
  temporal=$(mktemp "$STATE/$variable.XXXXXX") && [ -f "$temporal" ] && [ ! -L "$temporal" ] || {
    log "reglas 1 y 3 sin borrar: falló mktemp o no existe el temporal $variable"; exit 1;
  }
  printf -v "$variable" '%s' "$temporal"
done
restos=$(find "$STATE" -mindepth 1 -maxdepth 1 -name 'borrando-*' -print 2> "$INV_ERR") || {
  log "reglas 1 y 3 sin borrar: falló find de restos borrando-* en STATE"; exit 1;
}
if [ -n "$restos" ]; then
  while IFS= read -r resto; do
    log "conservo resto previo $resto: requiere autorización explícita"
  done <<< "$restos"
fi
# MARCA precede al inventario: perderla impide descartar corridas nuevas.
INV_OK=0
if ! inventario_dir=$(cd -P -- "$(dirname "$0")" && pwd -P); then
  INV_MOTIVO="falló cd -P o pwd -P al localizar el inventario"
elif [ ! -f "$MARCA" ] || [ -L "$MARCA" ]; then
  INV_MOTIVO="MARCA no existe o no es regular"
elif [ ! -d "$RR" ]; then
  INV_MOTIVO="no existe $RR"
elif ! /usr/bin/python3 -B "$inventario_dir/pao-inventario.py" --raiz "$RR" > "$INV_JSON" 2> "$INV_ERR"; then
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


# Una preparación reciente aún no tiene mirror en meta: no se sabe qué copia usa.
for c in datos['corridas']:
    if c['clase'] == 'preparacion-abortada' and datos['ts'] - (c['ultimoUso'] or 0) < 3600:
        print('sin-copias\t' + nombre(c['ruta'], runs))
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
INVENTARIO="${inventario_dir:-}/pao-inventario.py"
[ "$INV_OK" = 1 ] || log "reglas 1 y 3 sin borrar: $INV_MOTIVO"

# Rutas en uso: cwd de todos mis procesos, cualquier --basetemp en línea de
# comandos y todos los archivos que tengo abiertos.
# Un fallo de esta captura deja sin borrar sólo las reglas 1 y 3; las demás
# siguen con la captura parcial, como antes de este cambio.
sin_uso() { log "reglas 1 y 3 sin borrar: $1"; INV_OK=0; }
if ! uid=$(id -u); then
  sin_uso "falló id para EN_USO"; uid=-1
fi
if ! lsof -a -u "$uid" -d cwd -Fn > "$USO_CWD" 2> "$INV_ERR" || [ -s "$INV_ERR" ]; then
  sin_uso "falló lsof de cwd para EN_USO o emitió diagnósticos"
fi
if ! ps -axww -o command= > "$USO_PS" 2> "$INV_ERR" || [ -s "$INV_ERR" ]; then
  sin_uso "falló ps para EN_USO o emitió diagnósticos"
fi
if ! lsof -u "$uid" -Fn > "$USO_ABIERTOS" 2> "$INV_ERR" || [ -s "$INV_ERR" ]; then
  sin_uso "falló lsof de archivos para EN_USO o emitió diagnósticos"
fi
grep -o -- '--basetemp[= ][^ ]*' "$USO_PS" > "$USO_BASETEMP"; estado_basetemp=$?
if [ "$estado_basetemp" -gt 1 ]; then
  sin_uso "falló extracción de basetemp para EN_USO"
fi
if ! (set -o pipefail
  { sed -n 's/^n//p' "$USO_CWD" &&
    sed 's/^--basetemp[= ]//; s/"//g' "$USO_BASETEMP" &&
    sed -n 's/^n\(\/.*\)/\1/p' "$USO_ABIERTOS";
  } | sed 's#^/tmp/#/private/tmp/#' | sort -u > "$EN_USO"
); then
  sin_uso "falló captura o filtrado de EN_USO"
fi

en_uso() { # Un error al leer la captura también conserva la candidata.
  local estado
  grep -qF -- "$1" "$EN_USO"; estado=$?
  [ "$estado" = 1 ] && return 1
  [ "$estado" = 0 ] || log "no borro $1: falló lectura de EN_USO (estado $estado)"
  return 0
}

libre_ahora() { # sin lock y sin procesos dentro, con un lsof hecho justo antes de borrar
  if [ -e "$1/.remote-run.lock" ] || [ -L "$1/.remote-run.lock" ]; then
    log "no borro $1: tiene lock"; return 1
  fi
  local salida estado
  salida=$(lsof -nP +D "$1" 2>&1); estado=$?
  [ "$estado" = 1 ] && [ -z "$salida" ] && return 0
  log "no borro $1: lsof salió $estado: $(printf '%s\n' "$salida" | head -1 | cut -c1-200)"
  return 1
}

# 1. remote-run/work: copias no ocultas que el inventario marca borrables
if [ "$INV_OK" = 1 ] && { [ ! -f "$MARCA" ] || [ -L "$MARCA" ]; }; then
  INV_OK=0
  log "reglas 1 y 3 sin borrar: MARCA no existe o no es regular"
fi
if [ "$INV_OK" = 1 ]; then
  preparacion=$(grep -m1 '^sin-copias' "$INV_LISTA"); estado_preparacion=$?
  if [ "$estado_preparacion" -gt 1 ]; then
    INV_OK=0
    log "reglas 1 y 3 sin borrar: falló lectura de la lista del inventario"
  elif [ "$estado_preparacion" = 0 ]; then
    log "regla 1 sin borrar: preparación de menos de 1 h en runs/${preparacion#*$'\t'}"
  else
    while IFS=$'\t' read -r tipo n; do
      [ "$tipo" = work ] || continue
      case "$n" in ""|.*|*/*) continue ;; esac
      borrar_en "$RR/work" "$n" "inventario: inactiva y borrable" copia
      [ "$INV_OK" = 1 ] || break
    done < "$INV_LISTA"
  fi
fi

# 3. remote-run/runs: terminales que el inventario marca borrables (más de 30 días).
# Va justo tras la 1 para usar el mismo inventario recién tomado.
if [ "$INV_OK" = 1 ] && { [ ! -f "$MARCA" ] || [ -L "$MARCA" ]; }; then
  INV_OK=0
  log "reglas 1 y 3 sin borrar: MARCA no existe o no es regular"
fi
if [ "$INV_OK" = 1 ]; then
  while IFS=$'\t' read -r tipo n; do
    [ "$tipo" = runs ] || continue
    case "$n" in ""|.*|*/*) continue ;; esac
    r="$RR/runs/$n"
    [ -d "$r" ] && [ ! -L "$r" ] && [ -e "$r/exit" ] || continue
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
