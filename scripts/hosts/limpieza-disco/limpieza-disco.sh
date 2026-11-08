#!/bin/bash
# Limpieza automática de espacio regenerable. La ejecuta launchd cada hora
# (~/Library/LaunchAgents/com.cosmonaut.limpieza-disco.plist).
# Uso manual: limpieza-disco.sh [--dry-run]
#
# Modo emergencia: si al empezar quedan menos de 15 GB libres, los umbrales de
# antigüedad se acortan (entre paréntesis, el valor en emergencia).
# Sólo work tiene otra emergencia: <40 GB, ventana de 6 h y freno en 60 GB.
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
# Canal de log separado: ni las sustituciones de comando ni INV_ERR ocultan
# un timeout. El hijo no hereda este descriptor ni los pipes del llamador.
exec 3>&1
RECORRIDO_SEGUNDOS=600
acotado() { # regla, ruta, comando...; descarta stdout de recorridos fallidos
  /usr/bin/python3 -B -c '
import datetime, os, signal, subprocess, sys, tempfile
regla, ruta, limite = sys.argv[1:4]
with tempfile.TemporaryFile() as salida, tempfile.TemporaryFile() as errores:
    try:
        p = subprocess.Popen(sys.argv[4:], stdout=salida, stderr=errores,
                             start_new_session=True, close_fds=True)
        try:
            rc = p.wait(timeout=float(limite))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Un hijo en espera de E/S del kernel puede tardar en morir. No
            # esperamos sin límite ni dejamos que retenga un pipe de la pasada.
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            os.write(3, (str(datetime.datetime.now().replace(microsecond=0)) +
                         " tope " + limite + " s: " + regla + " ruta " + ruta + "\n").encode())
            rc = 124
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(125)
    # lsof usa 1 para ausencia de archivos. Conservar su stdout permite
    # rechazar una captura parcial incluso si devuelve ese código.
    if rc == 0 or (sys.argv[4] == "lsof" and rc == 1):
        salida.seek(0)
        while True:
            bloque = salida.read(65536)
            if not bloque: break
            sys.stdout.buffer.write(bloque)
    errores.seek(0)
    while True:
        bloque = errores.read(65536)
        if not bloque: break
        sys.stderr.buffer.write(bloque)
    sys.exit(rc if rc >= 0 else 128 - rc)
' "$1" "$2" "$RECORRIDO_SEGUNDOS" "${@:3}"
}
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
  if ! acotado "reglas 1 y 3: corridas nuevas" "$RR/runs" find "$RR/runs" -mindepth 1 -maxdepth 1 -newer "$MARCA" -print0 > "$RUNS_NUEVAS" 2> "$INV_ERR"; then
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
        mirror = inventario['espejo'](ruta / 'meta')
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
      actividad_find=("${COPIA_FIND[@]}")
      if [ "$WORK_EMERG" = 1 ]; then
        # -path interpreta globs aunque su argumento vaya entre comillas.
        # El nombre de la copia es literal; sólo *.lock es un patrón.
        patron_copia=${2//\\/\\\\}
        patron_copia=${patron_copia//\*/\\*}
        patron_copia=${patron_copia//\?/\\?}
        patron_copia=${patron_copia//\[/\\[}
        actividad_find+=(! \( -type d -path "./$patron_copia/.git" \)
          ! \( -type f \( -path "./$patron_copia/.git/index"
            -o -path "./$patron_copia/.git/FETCH_HEAD" -o -path "./$patron_copia/.git/ORIG_HEAD"
            -o \( -path "./$patron_copia/.git/*.lock" ! -path "./$patron_copia/.git/*/*" \) \) \))
      fi
      reciente=$(acotado "regla 1: actividad" "$1/$2" find "./$2" "${actividad_find[@]}" -print -quit 2> "$INV_ERR") || {
        log "no borro $1/$2: falló find de actividad de la copia"; exit 1;
      }
      [ -z "$reciente" ] || { log "no borro $1/$2: algo cambió en los últimos $COPIA_VENTANA"; exit 1; }
      if [ "$WORK_EMERG" = 1 ]; then
        cwd_work_libre "$1/$2" || { log "no borro $1/$2: cwd visible dentro o consulta no verificable"; exit 1; }
      fi
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
      (( ahora - mtime >= COPIA_MIN * 60 )) || {
        log "no borro $1/$2: el mtime directo tiene menos de $COPIA_VENTANA"; exit 1;
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

# Al soltar basta el pid propio; la recuperación sí verifica inicio y comando.
soltar_lock() {
  /usr/bin/python3 -B - "$LOCK" "$$" <<'PY_UNLOCK'
import datetime
import fcntl
import os
import stat
import sys
import time

lock, propio = sys.argv[1:]
try:
    guard = os.open(lock + '.guard', os.O_RDWR | os.O_NOFOLLOW)
    plazo = time.monotonic() + 10
    while True:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            restante = plazo - time.monotonic()
            if restante <= 0:
                raise TimeoutError('guard ocupado durante 10 s')
            time.sleep(min(0.1, restante))
    if not stat.S_ISREG(os.lstat(lock + '/owner').st_mode):
        raise ValueError('dueño no regular')
    with open(lock + '/owner') as archivo:
        pid, inicio, comando = archivo.read(16384).splitlines()
    if pid != propio:
        raise ValueError('pid de otra pasada')
    os.unlink(lock + '/owner')
    os.rmdir(lock)
except (OSError, ValueError) as exc:
    print(str(datetime.datetime.now().replace(microsecond=0)) + ' no suelto lock: ' + str(exc),
          flush=True)
PY_UNLOCK
}
# flock sólo serializa adquirir/recuperar y publicar owner; se libera incluso
# si el recuperador muere. El mkdir sigue siendo compatible con instalar.sh.
/usr/bin/python3 -B - "$LOCK" "$$" <<'PY_LOCK'
import datetime
import fcntl
import os
import stat
import subprocess
import sys
import time

lock, propio = sys.argv[1:]
adquirido = False


def log(mensaje):
    print(str(datetime.datetime.now().replace(microsecond=0)) + ' ' + mensaje, flush=True)


def ps(pid, campo):
    return subprocess.check_output(['ps', '-p', str(pid), '-o', campo + '='],
                                   env=dict(os.environ, LC_ALL='C', TZ='UTC0'),
                                   stderr=subprocess.PIPE, text=True, timeout=5).rstrip('\n')


def dueno_vivo():
    try:
        if not stat.S_ISREG(os.lstat(lock + '/owner').st_mode):
            raise ValueError('dueño no regular')
        with open(lock + '/owner') as archivo:
            pid, inicio, comando = archivo.read(16384).splitlines()
        if not pid.isdigit() or int(pid) <= 0 or not inicio or not comando:
            raise ValueError('dueño incompleto')
    except (OSError, ValueError):
        # El lock legado no tiene dueño. Nunca robar una limpieza anterior.
        captura = subprocess.check_output(['ps', '-axww', '-o', 'pid=,command='],
                                          stderr=subprocess.PIPE, text=True, timeout=5)
        return any(len(c) == 2 and c[0] != propio and 'limpieza-disco.sh' in c[1]
                   for c in (linea.split(None, 1) for linea in captura.splitlines()))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # EPERM no acredita identidad: comprobar inicio y comando por ps.
    # Una consulta fallida sube al cierre conservador de abajo.
    return (ps(pid, 'lstart') == inicio and ps(pid, 'command') == comando
            and 'limpieza-disco.sh' in comando)


try:
    guard = os.open(lock + '.guard', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('otra adquisición de lock en curso; salgo sin borrar')
        sys.exit(10)
    try:
        os.mkdir(lock)
    except FileExistsError:
        info = os.lstat(lock)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('lock no es un directorio regular')
        if time.time() - info.st_mtime <= 7200:
            log('otra limpieza en curso; salgo')
            sys.exit(10)
        if dueno_vivo():
            log('lock de más de 120 min con dueño vivo o no descartable; salgo sin borrar')
            sys.exit(10)
        try:
            os.unlink(lock + '/owner')
        except FileNotFoundError:
            pass
        os.rmdir(lock)
        os.mkdir(lock)
        log('recuperado lock de más de 120 min sin dueño vivo')
    adquirido = True
    inicio, comando = ps(propio, 'lstart'), ps(propio, 'command')
    if not inicio or not comando:
        raise ValueError('identidad de la limpieza no verificable')
    with open(lock + '/owner', 'x') as archivo:
        archivo.write(propio + '\n' + inicio + '\n' + comando + '\n')
except (OSError, ValueError, subprocess.SubprocessError) as exc:
    log('sin borrar: no pude verificar/adquirir el lock de limpieza: ' + str(exc))
    if adquirido:
        try:
            os.unlink(lock + '/owner')
        except FileNotFoundError:
            pass
        os.rmdir(lock)
    sys.exit(1)
PY_LOCK
estado_lock=$?
[ "$estado_lock" = 10 ] && exit 0
[ "$estado_lock" = 0 ] || exit "$estado_lock"
trap soltar_lock EXIT

antes=$(libre_gb)
[[ "$antes" =~ ^[0-9]+$ ]] || { log "sin borrar: espacio libre no verificable"; exit 1; }
WORK_EMERG=0 COPIA_MIN=10080 COPIA_VENTANA="7 días"
COPIA_FIND=(-maxdepth 2 -mtime -7)
INV_ARGS=(--copia-minutos "$COPIA_MIN")
if [ "$antes" -lt 40 ]; then
  WORK_EMERG=1 COPIA_MIN=360 COPIA_VENTANA="360 minutos"
  # git status puede refrescar index mediante rename y con ello el mtime de
  # .git. No son uso. Sí recorremos sus otros hijos y todo el árbol de trabajo.
  # Los patrones relativos no excluyen archivos homónimos fuera de .git.
  COPIA_FIND=(-mmin -360)
  INV_ARGS=(--copia-minutos "$COPIA_MIN")
  log "EMERGENCIA work: libre ${antes} GB, sin cambios 6 h, objetivo 60 GB$([ "$DRY" = 1 ] && echo ', dry-run')"
fi

# Umbrales en minutos: normales y en emergencia (<15 GB libres).
if [ "$antes" -lt 15 ]; then
  EMERG=1; PYTEST_MIN=180; CODEX_MIN=1440; CLAUDE_MIN=1440
else
  EMERG=0; PYTEST_MIN=720; CODEX_MIN=4320; CLAUDE_MIN=4320
fi
log "inicio (libre: ${antes} GB$([ "$EMERG" = 1 ] && echo ", EMERGENCIA")$([ "$DRY" = 1 ] && echo ", dry-run"))"

# Reglas 1 y 3: el inventario decide qué es borrable en ~/remote-run.
# Instalado junto a este script; desde un temporal permite una pasada en seco previa.
MARCA= INV_JSON= INV_LISTA= INV_ERR= EN_USO= USO_CWD= USO_ABIERTOS= USO_PS= USO_BASETEMP= RUNS_NUEVAS= CWD_WORK=
INV_OK=1 USO_OK=1 INV_MOTIVO=
sin_uso() {
  log "reglas 1, 2, 3, 5, 6 y 7 sin borrar: $1"
  INV_OK=0; USO_OK=0
}
limpiar_temporales() {
  soltar_lock
  for temporal in "$MARCA" "$INV_JSON" "$INV_LISTA" "$INV_ERR" "$EN_USO" \
      "$USO_CWD" "$USO_ABIERTOS" "$USO_PS" "$USO_BASETEMP" "$RUNS_NUEVAS" "$CWD_WORK"; do
    [ -z "$temporal" ] || rm -f -- "$temporal"
  done
}
trap limpiar_temporales EXIT
for variable in MARCA INV_JSON INV_LISTA INV_ERR EN_USO USO_CWD USO_ABIERTOS USO_PS USO_BASETEMP RUNS_NUEVAS; do
  temporal=$(mktemp "$STATE/$variable.XXXXXX") && [ -f "$temporal" ] && [ ! -L "$temporal" ] || {
    case "$variable" in
      INV_ERR|EN_USO|USO_*) sin_uso "falló mktemp o no existe el temporal $variable" ;;
      *) log "reglas 1 y 3 sin borrar: falló mktemp o no existe el temporal $variable"; INV_OK=0 ;;
    esac
    continue
  }
  printf -v "$variable" '%s' "$temporal"
done
restos=$(acotado "reglas 1 y 3: restos" "$STATE" find "$STATE" -mindepth 1 -maxdepth 1 -name 'borrando-*' -print 2> "${INV_ERR:-/dev/null}") || {
  log "reglas 1 y 3 sin borrar: falló find de restos borrando-* en STATE"; INV_OK=0;
}
if [ -n "$restos" ]; then
  while IFS= read -r resto; do
    log "conservo resto previo $resto: requiere autorización explícita"
  done <<< "$restos"
fi
# MARCA precede al inventario: perderla impide descartar corridas nuevas.
if [ "$INV_OK" != 1 ]; then
  :
elif ! inventario_dir=$(cd -P -- "$(dirname "$0")" && pwd -P); then
  INV_MOTIVO="falló cd -P o pwd -P al localizar el inventario"
elif [ ! -f "$MARCA" ] || [ -L "$MARCA" ]; then
  INV_MOTIVO="MARCA no existe o no es regular"
elif [ ! -d "$RR" ]; then
  INV_MOTIVO="no existe $RR"
elif ! acotado "reglas 1 y 3: inventario" "$RR" /usr/bin/python3 -B "$inventario_dir/pao-inventario.py" --raiz "$RR" "${INV_ARGS[@]}" > "$INV_JSON" 2> "$INV_ERR"; then
  INV_MOTIVO="el inventario falló: $(tr '\n' ' ' < "$INV_ERR" | cut -c1-300)"
elif ! /usr/bin/python3 -B - "$INV_JSON" "$RR" "$WORK_EMERG" "$COPIA_MIN" > "$INV_LISTA" 2> "$INV_ERR" <<'PY'
from datetime import datetime, timezone
import json
import math
import os
import sys

datos = json.load(open(sys.argv[1]))
raiz = os.path.abspath(sys.argv[2])
if datos.get('schema') != 'remote-run-inventario@1' or os.path.abspath(datos.get('raiz', '')) != raiz:
    sys.exit('esquema o raíz inesperados')
emergencia = sys.argv[3] == '1'
if datos['reglas']['copiaDias'] * 1440 != int(sys.argv[4]):
    sys.exit('umbral de copias inesperado')
if datos['proteccionCopias']['bloqueoGlobal']:
    sys.exit('bloqueo global del inventario: ' + repr(datos['proteccionCopias']['bloqueoGlobal']))
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
copias = datos['copias']
if emergencia:
    copias = sorted(copias, key=lambda c: (c['ultimoUso'] or 0, c['ruta']))
for c in copias:
    if c['borrable'] is True and c['clase'] == 'inactiva' and visible(c):
        fila = 'work\t' + nombre(c['ruta'], work)
        if emergencia:
            ultimo = c['ultimoUso']
            if not math.isfinite(ultimo):
                sys.exit('último uso de copia no verificable')
            fila += '\t' + datetime.fromtimestamp(ultimo, timezone.utc).isoformat()
        print(fila)
for c in datos['corridas']:
    if c['borrable'] is True and c['clase'] == 'terminal' and visible(c):
        print('runs\t' + nombre(c['ruta'], runs))
PY
then
  INV_MOTIVO="salida del inventario no válida: $(tr '\n' ' ' < "$INV_ERR" | cut -c1-300)"
fi
INVENTARIO="${inventario_dir:-}/pao-inventario.py"
if [ -n "$INV_MOTIVO" ]; then
  INV_OK=0
  log "reglas 1 y 3 sin borrar: $INV_MOTIVO"
fi

# Rutas en uso: cwd de todos mis procesos, cualquier --basetemp en línea de
# comandos y todos los archivos que tengo abiertos.
# Una captura parcial no prueba ausencia de uso: veta todas las reglas que la consultan.
if ! uid=$(id -u); then
  sin_uso "falló id para EN_USO"; uid=-1
fi
if ! acotado "captura cwd" "$HOME" lsof -a -u "$uid" -d cwd -Fn > "$USO_CWD" 2> "${INV_ERR:-/dev/null}" || [ -s "$INV_ERR" ]; then
  sin_uso "falló lsof de cwd para EN_USO o emitió diagnósticos"
fi
if ! ps -axww -o command= > "$USO_PS" 2> "${INV_ERR:-/dev/null}" || [ -s "$INV_ERR" ]; then
  sin_uso "falló ps para EN_USO o emitió diagnósticos"
fi
if ! acotado "captura abiertos" "$HOME" lsof -u "$uid" -Fn > "$USO_ABIERTOS" 2> "${INV_ERR:-/dev/null}" || [ -s "$INV_ERR" ]; then
  sin_uso "falló lsof de archivos para EN_USO o emitió diagnósticos"
fi
if ! { grep -o -- '--basetemp[= ][^ ]*' "$USO_PS"; e=$?; [ "$e" -le 1 ]; } > "$USO_BASETEMP"; then
  sin_uso "falló extracción o escritura de basetemp para EN_USO"
fi
filtrar_uso() {
  local fuente="$1" expresion="$2" archivo="$3"
  local estados
  sed -n "$expresion" "$archivo" | sed 's#^/tmp/#/private/tmp/#' | sort -u >> "$EN_USO"
  estados=("${PIPESTATUS[@]}")
  [ "${estados[0]}" = 0 ] || sin_uso "falló sed de $fuente para EN_USO"
  [ "${estados[1]}" = 0 ] || sin_uso "falló sed de normalización de $fuente para EN_USO"
  [ "${estados[2]}" = 0 ] || sin_uso "falló sort o escritura de EN_USO al filtrar $fuente"
}
filtrar_uso cwd 's/^n//p' "$USO_CWD"
filtrar_uso basetemp 's/^--basetemp[= ]//; s/"//g; p' "$USO_BASETEMP"
filtrar_uso 'archivos abiertos' 's/^n\(\/.*\)/\1/p' "$USO_ABIERTOS"

en_uso() { # Un error al leer la captura también conserva la candidata.
  local estado
  [ "$USO_OK" = 1 ] || return 0
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
  salida=$(acotado "reglas 1 y 3: lsof previo" "$1" lsof -nP +D "$1" 2>&1); estado=$?
  [ "$estado" = 1 ] && [ -z "$salida" ] && return 0
  log "no borro $1: lsof salió $estado: $(printf '%s\n' "$salida" | head -1 | cut -c1-200)"
  return 1
}

# Captura adicional sólo para work en emergencia: cobertura del propio uid.
# Validar toda la estructura y cobertura antes de la primera candidata.
# No cambia EN_USO ni las reglas restantes. Cualquier fallo veta toda la regla 1.
WORK_CWD_OK=0
if [ "$WORK_EMERG" = 1 ]; then
  if CWD_WORK=$(mktemp "$STATE/CWD_WORK.XXXXXX") && [ -f "$CWD_WORK" ] && [ ! -L "$CWD_WORK" ]; then
    if motivo_cwd=$(acotado "regla 1: captura cwd completa" "$RR" /usr/bin/python3 -B - "$CWD_WORK" "$RR" "$uid" 2>&1 <<'PY_CWD'
import json
import os
import re
import stat
import subprocess
import sys


def consultar(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=30)
    if r.returncode != 0 or r.stderr:
        raise ValueError('falló consulta ' + args[0] + ' o emitió diagnósticos')
    return r.stdout


def pids_propios(uid):
    salida = consultar(['ps', '-U', str(uid), '-o', 'pid=,stat='])
    pids = set()
    vistos = set()
    for linea in salida.splitlines():
        campos = linea.split()
        if (len(campos) != 2 or not re.fullmatch(r'[1-9][0-9]*', campos[0])
                or not re.fullmatch(r'[A-Z][A-Za-z0-9+<>-]*', campos[1])):
            raise ValueError('captura ps no verificable')
        pid = int(campos[0])
        if pid in vistos:
            raise ValueError('pid repetido en captura ps')
        vistos.add(pid)
        # Un zombie ya no tiene cwd; no es un proceso vivo que use una copia.
        if not campos[1].startswith('Z'):
            pids.add(pid)
    if not vistos:
        raise ValueError('captura ps vacía')
    return pids


try:
    uid = int(sys.argv[3])
    if uid < 0:
        raise ValueError('uid no verificable')
    for ruta in (sys.argv[2], os.path.join(sys.argv[2], 'work')):
        info = os.lstat(ruta)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o055:
            raise ValueError('permisos no privados (lectura/ejecución de group u other) o directorio no regular: ' + ruta)
    # Antes de lsof: los procesos nacidos durante/después no son omisiones
    # de esta captura. Los que terminan durante ella se vuelven a comprobar.
    esperados = pids_propios(uid)
    captura = consultar(['lsof', '-a', '-d', 'cwd', '-Fn']).splitlines()
    if not captura or len(captura) % 3:
        raise ValueError('captura de cwd incompleta o vacía')
    pids = set()
    rutas = []
    for i in range(0, len(captura), 3):
        proceso, descriptor, nombre = captura[i:i + 3]
        if (not re.fullmatch(r'p[1-9][0-9]*', proceso)
                or descriptor != 'fcwd' or not nombre.startswith('n/')):
            raise ValueError('captura de cwd con registro incompleto o campos sueltos')
        pid = int(proceso[1:])
        ruta = nombre[1:]
        # lsof escapa caracteres no imprimibles; no adivinar esas rutas.
        if (pid in pids or '\\' in ruta or ruta.endswith(' (deleted)')
                or any(ord(c) < 32 or ord(c) == 127 for c in ruta)):
            raise ValueError('captura de cwd con pid repetido o ruta no verificable')
        pids.add(pid)
        rutas.append(os.path.realpath(ruta))
    ausentes = esperados - pids
    if ausentes:
        vivos_ausentes = ausentes & pids_propios(uid)
        if vivos_ausentes:
            raise ValueError('cobertura de cwd incompleta para uid {}: pids vivos ausentes {}'.format(
                uid, ','.join(map(str, sorted(vivos_ausentes)))))
    # Sólo se publica una captura normalizada después de validar el conjunto.
    with open(sys.argv[1], 'w', encoding='utf-8') as archivo:
        json.dump(rutas, archivo)
except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
    sys.exit(str(exc))
PY_CWD
    ); then
      WORK_CWD_OK=1
    else
      log "EMERGENCIA work sin borrar: ${motivo_cwd%%$'\n'*}"
    fi
  else
    log "EMERGENCIA work sin borrar: falló temporal para captura de cwd"
  fi
fi
cwd_work_libre() {
  [ "$WORK_CWD_OK" = 1 ] || return 1
  /usr/bin/python3 -B - "$CWD_WORK" "$1" <<'PY'
import json
import os
import sys

try:
    copia = os.path.realpath(sys.argv[2])
    with open(sys.argv[1], encoding='utf-8') as captura:
        rutas = json.load(captura)
    if not isinstance(rutas, list) or not rutas or any(not isinstance(r, str) or not r.startswith('/') for r in rutas):
        raise ValueError('captura de cwd normalizada no verificable')
    sys.exit(1 if any(r == copia or r.startswith(copia + os.sep) for r in rutas) else 0)
except (OSError, ValueError, UnicodeError):
    sys.exit(2)
PY
}

freno_work() {
  if [ "$work_libre" -ge 60 ]; then
    log "EMERGENCIA work: freno en ${work_libre} GB (df)"; return 0
  fi
  return 1
}

# 1. remote-run/work: copias no ocultas que el inventario marca borrables
work_bytes=0 work_cantidad=0 work_libre=$antes
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
    while IFS=$'\t' read -r tipo n ultimo; do
      [ "$tipo" = work ] || continue
      case "$n" in ""|.*|*/*) continue ;; esac
      motivo="inventario: inactiva y borrable"
      if [ "$WORK_EMERG" = 1 ]; then
        [ "$WORK_CWD_OK" = 1 ] || break
        work_libre=$(libre_gb)
        [[ "$work_libre" =~ ^[0-9]+$ ]] || { log "EMERGENCIA work sin borrar: espacio libre no verificable"; break; }
        freno_work && break
        # Medir sólo esta copia: --tamanos en el inventario también recorrería
        # runs/ y podría alterar la clasificación de la regla 3 ante un error.
        tamano=$(acotado "regla 1: tamaño" "$RR/work/$n" /usr/bin/python3 -B - "$INVENTARIO" "$RR/work/$n" 2> "$INV_ERR" <<'PY'
from pathlib import Path
import runpy
import sys

print(runpy.run_path(sys.argv[1])['tamano'](Path(sys.argv[2])))
PY
        ) && [[ "$tamano" =~ ^[0-9]+$ ]] && [ ! -s "$INV_ERR" ] || {
          log "no borro $RR/work/$n: tamaño no verificable"; continue;
        }
        motivo="EMERGENCIA work; tamaño $tamano bytes; último uso $ultimo"
      fi
      if borrar_en "$RR/work" "$n" "$motivo" copia && [ "$WORK_EMERG" = 1 ]; then
        work_bytes=$((work_bytes + tamano)); work_cantidad=$((work_cantidad + 1))
        # Medir tras cada borrado, incluso el último. Los bloques de clones
        # APFS no predicen espacio recuperado: sólo df decide el objetivo.
        work_libre=$(libre_gb)
        [[ "$work_libre" =~ ^[0-9]+$ ]] || { log "EMERGENCIA work: paro, espacio libre no verificable"; break; }
        freno_work && break
      fi
      [ "$INV_OK" = 1 ] || break
    done < "$INV_LISTA"
  fi
fi
if [ "$WORK_EMERG" = 1 ]; then
  work_gb=$(awk -v bytes="$work_bytes" 'BEGIN {printf "%.3f", bytes / 1073741824}')
  if [ "$DRY" = 1 ]; then
    log "EMERGENCIA work: $work_cantidad copias; total $work_bytes bytes (${work_gb} GB) de copias candidatas, dry-run; recuperación real no predecible; df ${work_libre} GB (antes ${antes} GB)"
  else
    log "EMERGENCIA work: $work_cantidad copias; total $work_bytes bytes (${work_gb} GB) de copias borradas; libre ${work_libre} GB (antes ${antes} GB)"
    if [[ "$work_libre" =~ ^[0-9]+$ ]]; then
      log "EMERGENCIA work: total liberado observado $((work_libre - antes)) GB (df, resolución 1 GB; incluye actividad concurrente)"
    fi
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
      if ! popen=$(acotado "regla 2: basetemp" "$d" find "$d" -mindepth 1 -maxdepth 1 -type d -name 'popen-gw*' -print -quit 2>/dev/null); then
        continue
      elif [ -n "$popen" ]; then
        motivo="basetemp de pytest-xdist"
      elif [[ "$d" == /private/tmp/wt-* ]] && [ -z "$(ls -A "$d" 2>/dev/null | grep -vx '.DS_Store')" ]; then
        motivo="carpeta wt-* vacía"
      else
        continue
      fi ;;
  esac
  reciente=$(acotado "regla 2: actividad" "$d" find "$d" -maxdepth 3 -mmin -"$PYTEST_MIN" -print -quit 2>/dev/null) || continue
  [ -n "$reciente" ] && continue
  en_uso "$d" && continue
  borrar "$d" "$motivo"
done

# 4. Caché de Bun: semanal y sólo si pasa de 10 GB
BUN_CACHE="$HOME/.bun/install/cache"
STAMP="$STATE/bun-cache-stamp"
if [ -d "$BUN_CACHE" ] && { [ ! -e "$STAMP" ] || [ -n "$(acotado "regla 4: sello" "$STAMP" find "$STAMP" -mtime +6)" ]; }; then
  medida=$(acotado "regla 4: tamaño" "$BUN_CACHE" du -skx "$BUN_CACHE" 2>/dev/null) || medida=
  kb=${medida%%$'\t'*}
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
  reciente=$(acotado "regla 5: actividad" "$wt" find "$wt" -maxdepth 0 -mmin -"$CODEX_MIN") || continue
  [ -n "$reciente" ] && continue
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
  reciente=$(acotado "regla 6: actividad" "$s" find "$s" -mmin -"$CLAUDE_MIN" -print -quit 2>/dev/null) || continue
  [ -n "$reciente" ] && continue
  en_uso "$s" && continue
  borrar "$s" "scratchpad de Claude inactivo"
done

# 7. Cachés de Next.js (.next/cache y .next/dev) de apps sin servidor vivo
# Glob de primer nivel, incluidas ocultas: ninguna raíz bloquea a las demás.
for raiz in "$HOME"/* "$HOME"/.[!.]* "$HOME"/..?*; do
  [ ! -L "$raiz" ] && [ -d "$raiz" ] || continue
  case "$raiz" in
    "$HOME/Documents"|"$HOME/Desktop"|"$HOME/OrbStack"|"$HOME/Library"|"$HOME/.Trash"|\
    "$HOME/.cache"|"$HOME/.bun"|"$HOME/.rustup"|"$HOME/.android"|"$HOME/.cargo"|"$HOME/remote-run") continue ;;
  esac
  # Listar xattrs también puede bloquear: el mismo tope y fallo cerrado que find.
  atributos=$(acotado "regla 7: xattr" "$raiz" xattr "$raiz" 2>/dev/null) || {
    log "regla 7: salto raíz $raiz: falló xattr"; continue;
  }
  case $'\n'"$atributos"$'\n' in
    *$'\ncom.apple.macl\n'*) log "regla 7: salto raíz $raiz: com.apple.macl"; continue ;;
    *$'\ncom.apple.file-provider-domain-id\n'*) log "regla 7: salto raíz $raiz: com.apple.file-provider-domain-id"; continue ;;
  esac
  acotado "regla 7: candidatas" "$raiz" find "$raiz" -xdev \( -name node_modules -o -name .git \
      -o -path "$HOME/.local/state" \) -prune -o -type d -name .next -print -prune 2>/dev/null |
while IFS= read -r nx; do
  app=$(dirname "$nx")
  # Un proceso con cwd en la app (p. ej. next dev) la protege entera.
  grep -qxF -- "$app" "$EN_USO" && continue
  for c in "$nx/cache" "$nx/dev"; do
    [ -d "$c" ] || continue
    en_uso "$c" && continue
    reciente=$(acotado "regla 7: actividad 30 min" "$c" find "$c" -mmin -30 -print -quit 2>/dev/null) || continue
    [ -n "$reciente" ] && continue
    medida=$(acotado "regla 7: tamaño" "$c" du -skx "$c" 2>/dev/null) || continue
    kb=${medida%%$'\t'*}
    [[ "$kb" =~ ^[0-9]+$ ]] || continue
    if [ "$EMERG" = 0 ] && [ "$kb" -le $((1024 * 1024)) ]; then
      reciente=$(acotado "regla 7: actividad 1 día" "$c" find "$c" -mmin -1440 -print -quit 2>/dev/null) || continue
      [ -n "$reciente" ] && continue
    fi
    borrar "$c" "caché de Next.js, $((kb / 1024)) MB"
  done
done
done

# 8. Simuladores de Xcode no disponibles: semanal
STAMP_SIM="$STATE/simctl-stamp"
if command -v xcrun >/dev/null && { [ ! -e "$STAMP_SIM" ] || [ -n "$(acotado "regla 8: sello" "$STAMP_SIM" find "$STAMP_SIM" -mtime +6)" ]; }; then
  if [ "$DRY" = 1 ]; then
    log "[dry-run] ejecutaría xcrun simctl delete unavailable"
  else
    xcrun simctl delete unavailable >/dev/null 2>&1 && log "simuladores no disponibles borrados"
    touch "$STAMP_SIM"
  fi
fi

log "fin (libre: $(libre_gb) GB, antes: ${antes} GB)"
