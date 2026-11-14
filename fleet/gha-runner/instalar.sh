#!/usr/bin/env bash
# Runners de GitHub Actions de la Mini para los trabajos ligeros de Jale.
# Uso, en la Mini: instalar.sh [--seco|--retirar] guard|datos. El token de GitHub llega por stdin.
set -euo pipefail
modo=instalar
case "${1:-}" in --seco|--retirar) modo=$1; shift ;; esac
# Un runner por papel: el guard abre paso a Boat y no espera detrás de las unitarias.
papel=${1:-}
case "$#:$papel" in
  1:guard|1:datos) ;;
  *) echo 'Uso: instalar.sh [--seco|--retirar] guard|datos' >&2; exit 2 ;;
esac
version=2.338.2
sha256=25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388
repo=https://github.com/SEN-HASO/Jale
nombre=mini-jale-$papel
etiqueta=jale-$papel
label=local.pao.gha-runner-jale-$papel
dir="$HOME/actions-runner-jale-$papel"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/pao-gha-runner-jale-$papel.log"
paquete="actions-runner-osx-arm64-$version.tar.gz"
# Los trabajos reciben esta ruta fija; no heredan la del shell que instala.
ruta=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
dominio="gui/$(id -u)"
fecha=$(date +%Y%m%d-%H%M%S)-$$

fallo() { echo "gha-runner: $1" >&2; exit "${2:-1}"; }

leer_token() {
  token=
  [ -t 0 ] || IFS= read -r token || true
  [ -n "$token" ] || fallo "falta el token de $1 en stdin" 2
}

# El token va por entorno: en argv lo mostraría cualquier ps de la Mini.
configurar() { (cd "$dir" && PATH="$ruta" ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh "$@"); }

descargar() {
  launchctl print "$dominio/$label" >/dev/null 2>&1 || return 0
  launchctl bootout "$dominio/$label"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$dominio/$label" >/dev/null 2>&1 || return 0
    sleep 1
  done
  fallo 'launchd no soltó el servicio en 10 s'
}

case "$modo" in
  --seco)
    printf 'Descargar %s, comprobar su sha256 y extraerlo en %s\n' "$paquete" "$dir"
    printf 'Registrar %s en %s con la etiqueta %s\n' "$nombre" "$repo" "$etiqueta"
    printf 'Escribir %s y cargarlo en %s\n' "$plist" "$dominio"
    exit ;;
  --retirar)
    if [ -f "$dir/.runner" ]; then
      leer_token retiro
    else
      # Una instalación que falló a medias pudo registrar el runner sin dejar .runner.
      echo 'gha-runner: sin registro local que dar de baja; si el runner figura en el repositorio, bórralo en GitHub' >&2
    fi
    descargar
    # Si GitHub ya no tiene el registro, la baja falla; el retiro local sigue.
    if [ -f "$dir/.runner" ] && ! configurar remove; then
      echo 'gha-runner: GitHub no confirmó la baja; revisa que el runner ya no figure en el repositorio' >&2
    fi
    for destino in "$plist" "$dir"; do
      if [ -e "$destino" ]; then
        mv "$destino" "$destino.retirado-$fecha"
        printf 'Retirado: %s.retirado-%s\n' "$destino" "$fecha"
      fi
    done
    exit ;;
esac

[ "$(uname -sm)" = 'Darwin arm64' ] || fallo 'el paquete fijado es para macOS arm64'
if [ ! -f "$dir/.runner" ]; then
  leer_token registro
  mkdir -p "$dir"
  curl -fsSL -o "$dir/$paquete" "https://github.com/actions/runner/releases/download/v$version/$paquete"
  printf '%s  %s\n' "$sha256" "$dir/$paquete" | shasum -a 256 -c - >/dev/null \
    || fallo "$paquete no coincide con el sha256 fijado"
  tar -xzf "$dir/$paquete" -C "$dir"
  rm -f "$dir/$paquete"
  configurar --unattended --url "$repo" --name "$nombre" --labels "$etiqueta" --work _work --replace
fi

# La actualización automática del runner busca este proceso por su ruta y mantiene la copia al día.
cp "$dir/bin/runsvc.sh" "$dir/runsvc.sh"
chmod u+x "$dir/runsvc.sh"
mkdir -p "$(dirname "$plist")" "$(dirname "$log")"
# Misma forma que la plantilla del runner, con KeepAlive: launchd lo levanta si cae.
cat > "$plist.tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key><array><string>$dir/runsvc.sh</string></array>
    <key>WorkingDirectory</key><string>$dir</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$log</string>
    <key>StandardErrorPath</key><string>$log</string>
    <key>EnvironmentVariables</key><dict><key>ACTIONS_RUNNER_SVC</key><string>1</string></dict>
    <key>ProcessType</key><string>Interactive</string>
    <key>SessionCreate</key><true/>
</dict>
</plist>
PLIST
mv "$plist.tmp" "$plist"
descargar
touch "$log"
visto=$(wc -c < "$log")
launchctl bootstrap "$dominio" "$plist"
for _ in $(seq 1 "${PAO_GHA_RUNNER_ESPERA:-60}"); do
  if tail -c "+$((visto + 1))" "$log" | grep -q 'Listening for Jobs'; then
    printf 'Runner %s escuchando; etiqueta %s\n' "$nombre" "$etiqueta"
    exit
  fi
  sleep 1
done
fallo "el runner no llegó a escuchar; mira $log"
