#!/usr/bin/env bash
# Uso: instalar.sh [--seco|--retirar] guard|datos|hasito-noche|hasito-ci. Token por stdin.
set +x
set -euo pipefail
modo=instalar
case "${1:-}" in --seco|--retirar) modo=$1; shift ;; esac
papel=${1:-}
fallo() { echo "gha-runner: $1" >&2; exit "${2:-1}"; }
uso() { fallo 'Uso: instalar.sh [--seco|--retirar] guard|datos|hasito-noche|hasito-ci' 2; }
[ "$#" -eq 1 ] || uso

encontrado=0
while IFS='|' read -r clave sistemas repo nombre etiqueta carpeta label hook; do
  [ "$clave" = "$papel" ] || continue
  encontrado=1
  break
done <<'PAPELES'
guard|Darwin arm64|https://github.com/SEN-HASO/Jale|mini-jale-guard|jale-guard|actions-runner-jale-guard|local.pao.gha-runner-jale-guard|-
datos|Darwin arm64|https://github.com/SEN-HASO/Jale|mini-jale-datos|jale-datos|actions-runner-jale-datos|local.pao.gha-runner-jale-datos|-
hasito-noche|Darwin arm64|https://github.com/SEN-HASO/Hasito|mini-hasito-noche|hasito-noche|actions-runner-hasito-noche|local.pao.gha-runner-hasito-noche|.local/lib/pao-gha-runner/solo-main.sh
hasito-ci|Darwin arm64,Linux x86_64|https://github.com/SEN-HASO/Hasito|@host@-hasito-ci|hasito-ci|actions-runner-hasito-ci|local.pao.gha-runner-hasito-ci|-
PAPELES
[ "$encontrado" -eq 1 ] || uso

sistema=$(uname -sm)
case "$sistemas" in
  'Darwin arm64') destino='la Mini (macOS arm64)' ;;
  *) destino='la Mini (macOS arm64) o el desk (Linux x64)' ;;
esac
case ",$sistemas," in
  *",$sistema,"*) ;;
  *) fallo "el papel $papel requiere $destino; este sistema es $sistema" ;;
esac

version=2.338.2
dir="$HOME/$carpeta"
if [ "$hook" = - ]; then hook=; else hook="$HOME/$hook"; fi
case "$sistema" in
  'Darwin arm64')
    host=mini
    arquitectura=osx-arm64
    sha256=25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388
    servicio="$HOME/Library/LaunchAgents/$label.plist"
    log="$HOME/Library/Logs/pao-gha-runner-${label#local.pao.gha-runner-}.log"
    ruta=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
    dominio="gui/$(id -u)"
    ;;
  'Linux x86_64')
    host=desk
    arquitectura=linux-x64
    sha256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
    servicio="$HOME/.config/systemd/user/$label.service"
    log="$HOME/.local/state/pao-gha-runner/$papel.log"
    ruta=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
    dominio='systemd de usuario'
    ;;
esac
nombre=${nombre//@host@/$host}
paquete="actions-runner-$arquitectura-$version.tar.gz"
fecha=$(date +%Y%m%d-%H%M%S)-$$

leer_token() {
  token=
  [ -t 0 ] || IFS= read -r token || true
  [ -n "$token" ] || fallo "falta el token de $1 en stdin" 2
}

# config.sh no incluye el token entre las variables que guarda en .env.
configurar() { (cd "$dir" && PATH="$ruta" ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh "$@"); }

comprobar_dependencias() {
  local ldconfig cache lib sonames soname estado disponible faltantes=
  ldconfig=$(command -v ldconfig || printf /sbin/ldconfig)
  cache=$("$ldconfig" -p) || fallo 'no se pudo consultar ldconfig -p'
  while IFS='|' read -r lib sonames; do
    disponible=1
    estado=$(dpkg-query -W -f='${Status}' "$lib" 2>/dev/null) || estado=
    [ "$estado" = 'install ok installed' ] || disponible=0
    for soname in $sonames; do
      if ! printf '%s\n' "$cache" | awk -v nombre="$soname" \
        '$1 == nombre { encontrado=1 } END { exit !encontrado }'; then
        disponible=0
      fi
    done
    [ "$disponible" -eq 1 ] || faltantes="$faltantes $lib"
  done <<'BIBLIOTECAS'
libc6|libc.so.6
libgcc-s1|libgcc_s.so.1
libstdc++6|libstdc++.so.6
libgssapi-krb5-2|libgssapi_krb5.so.2
libkrb5-3|libkrb5.so.3
libicu74|libicuuc.so.74 libicui18n.so.74 libicudata.so.74
libssl3t64|libssl.so.3 libcrypto.so.3
zlib1g|libz.so.1
BIBLIOTECAS
  [ -z "$faltantes" ] || fallo "faltan dependencias de Ubuntu 24.04:$faltantes"
}

soltar_servicio() {
  case "$sistema" in
    'Darwin arm64')
      launchctl print "$dominio/$label" >/dev/null 2>&1 || return 0
      launchctl bootout "$dominio/$label" || return
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        launchctl print "$dominio/$label" >/dev/null 2>&1 || return 0
        sleep 1
      done
      fallo 'launchd no soltó el servicio en 10 s'
      ;;
    'Linux x86_64')
      if [ -f "$servicio" ] || systemctl --user is-active --quiet "$label.service"; then
        systemctl --user stop "$label.service" || return
        systemctl --user disable "$label.service" || return
      fi
      ;;
  esac
}

cargar_servicio() {
  local dir_xml log_xml unidad_dir unidad_log ejecutable
  carga_iniciada=1
  case "$sistema" in
    'Darwin arm64')
      dir_xml=$(printf '%s' "$dir" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
      log_xml=$(printf '%s' "$log" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
      cat > "$servicio.tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key><array><string>$dir_xml/runsvc.sh</string></array>
    <key>WorkingDirectory</key><string>$dir_xml</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$log_xml</string>
    <key>StandardErrorPath</key><string>$log_xml</string>
    <key>EnvironmentVariables</key><dict>
        <key>ACTIONS_RUNNER_SVC</key><string>1</string>
        <key>PATH</key><string>$ruta</string>
    </dict>
    <key>ProcessType</key><string>Interactive</string>
    <key>SessionCreate</key><true/>
</dict>
</plist>
PLIST
      mv "$servicio.tmp" "$servicio"
      launchctl bootstrap "$dominio" "$servicio"
      ;;
    'Linux x86_64')
      unidad_dir=${dir//%/%%}
      unidad_log=${log//%/%%}
      ejecutable=${unidad_dir//\\/\\\\}
      ejecutable=${ejecutable//\"/\\\"}
      ejecutable=${ejecutable//\$/\$\$}
      cat > "$servicio.tmp" <<UNIDAD
[Unit]
Description=Runner de CI de Hasito

[Service]
Type=simple
WorkingDirectory=$unidad_dir
ExecStart="$ejecutable/runsvc.sh"
Environment="ACTIONS_RUNNER_SVC=1"
Environment="PATH=$ruta"
Nice=10
CPUWeight=10
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=30
StandardOutput=append:$unidad_log
StandardError=append:$unidad_log

[Install]
WantedBy=default.target
UNIDAD
      mv "$servicio.tmp" "$servicio"
      systemctl --user daemon-reload
      systemctl --user enable --now "$label.service"
      ;;
  esac
}

instalar_hook() {
  local origen temporal linea
  origen="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/solo-main.sh"
  temporal="$hook.tmp-$fecha"
  [ ! -d "$hook" ] && mkdir -p "$(dirname "$hook")" &&
    cp "$origen" "$temporal" && chmod 0755 "$temporal" && mv "$temporal" "$hook" \
    || fallo 'no se pudo instalar el hook'
  if [ -e "$dir/.env" ] && { [ ! -f "$dir/.env" ] || [ ! -r "$dir/.env" ]; }; then
    fallo 'no se pudo leer .env para configurar el hook'
  fi
  temporal="$dir/.env.tmp-$fecha"
  {
    if [ -e "$dir/.env" ]; then
      while IFS= read -r linea || [ -n "$linea" ]; do
        case "$linea" in ACTIONS_RUNNER_HOOK_JOB_STARTED=*) continue ;; esac
        printf '%s\n' "$linea" || return 1
      done < "$dir/.env" || return 1
    fi
    printf 'ACTIONS_RUNNER_HOOK_JOB_STARTED=%s\n' "$hook"
  } > "$temporal" && mv "$temporal" "$dir/.env" \
    || fallo 'no se pudo configurar el hook en .env'
}

case "$modo" in
  --seco)
    if [ "$sistema" = 'Linux x86_64' ]; then
      printf 'Comprobar paquetes y bibliotecas de Ubuntu 24.04 con dpkg-query y ldconfig -p antes de registrar\n'
    fi
    printf 'Descargar %s, comprobar su sha256 y extraerlo en %s\n' "$paquete" "$dir"
    printf 'Registrar %s en %s con la etiqueta %s\n' "$nombre" "$repo" "$etiqueta"
    if [ -n "$hook" ]; then
      printf 'Copiar solo-main.sh con modo 0755 en %s de forma atómica\n' "$hook"
      printf 'Conservar %s/.env y escribir ACTIONS_RUNNER_HOOK_JOB_STARTED=%s sin duplicados\n' "$dir" "$hook"
    fi
    if [ "$papel" = hasito-ci ]; then
      printf 'Fijar PATH en el servicio y en %s/.path: %s\n' "$dir" "$ruta"
      [ "$sistema" != 'Linux x86_64' ] || printf 'Prioridad de CPU: Nice=10, CPUWeight=10; sin control de E/S efectivo en este host\n'
    fi
    printf 'Escribir %s y cargarlo en %s\n' "$servicio" "$dominio"
    exit ;;
  --retirar)
    if [ -f "$dir/.runner" ]; then
      leer_token retiro
    else
      echo 'gha-runner: sin registro local que dar de baja; si el runner figura en el repositorio, bórralo en GitHub' >&2
    fi
    soltar_servicio
    if [ -f "$dir/.runner" ] && ! configurar remove; then
      echo 'gha-runner: GitHub no confirmó la baja; revisa que el runner ya no figure en el repositorio' >&2
    fi
    for destino in "$servicio" "$dir" "$hook"; do
      if [ -e "$destino" ]; then
        mv "$destino" "$destino.retirado-$fecha"
        printf 'Retirado: %s.retirado-%s\n' "$destino" "$fecha"
      fi
    done
    [ "$sistema" != 'Linux x86_64' ] || systemctl --user daemon-reload
    exit ;;
esac

carga_iniciada=0
al_salir() {
  local codigo=$?
  if [ "$codigo" -ne 0 ] && { [ "$carga_iniciada" -eq 1 ] || [ -f "$servicio" ]; }; then
    soltar_servicio || echo 'gha-runner: no se pudo soltar el servicio tras el fallo' >&2
  fi
  return "$codigo"
}
trap al_salir EXIT

[ "$sistema" != 'Linux x86_64' ] || comprobar_dependencias
if [ ! -f "$dir/.runner" ]; then
  leer_token registro
  mkdir -p "$dir"
  curl -fsSL -o "$dir/$paquete" "https://github.com/actions/runner/releases/download/v$version/$paquete"
  printf '%s  %s\n' "$sha256" "$dir/$paquete" | shasum -a 256 -c - >/dev/null \
    || fallo "$paquete no coincide con el sha256 fijado"
  tar -xzf "$dir/$paquete" -C "$dir"
  rm -f "$dir/$paquete"
  configurar --unattended --url "$repo" --name "$nombre" --labels "$etiqueta" --work _work --replace
  unset token
fi

[ -z "$hook" ] || instalar_hook

# El actualizador del runner busca este proceso en la raíz, fuera de bin/.
cp "$dir/bin/runsvc.sh" "$dir/runsvc.sh"
chmod u+x "$dir/runsvc.sh"
# runsvc.sh lee .path y sustituye el PATH que recibe del gestor de servicios.
printf '%s\n' "$ruta" > "$dir/.path.tmp-$fecha"
mv "$dir/.path.tmp-$fecha" "$dir/.path"
mkdir -p "$(dirname "$servicio")" "$(dirname "$log")"
soltar_servicio
touch "$log"
visto=$(wc -c < "$log")
cargar_servicio
for _ in $(seq 1 "${PAO_GHA_RUNNER_ESPERA:-60}"); do
  if tail -c "+$((visto + 1))" "$log" | grep -q 'Listening for Jobs'; then
    printf 'Runner %s escuchando; etiqueta %s\n' "$nombre" "$etiqueta"
    exit
  fi
  sleep 1
done
fallo "el runner no llegó a escuchar; mira $log"
