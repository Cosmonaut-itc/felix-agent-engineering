# Vigía del rack de pruebas

En la MacBook, el LaunchAgent consulta cada tres minutos la vista privada del
rack. Avisa cuando observa un grupo en pausa en Pueue (`Paused`) con razón
`manual` o `ambiguous` y al menos una tarea `Queued` durante más de 30
minutos. Una razón guardada con el grupo en marcha no cuenta: `pausar` la
guarda antes de llamar a Pueue y la llamada puede fallar. Cuenta desde la primera
observación. Cada episodio tiene un aviso, con host, grupo, motivos, minutos y
tareas en fila. Una lectura válida sin esa combinación termina el episodio.

El rack ya reúne las razones de `capabilities` y la fila completa de cada host.
En el desk, `reorder_groups` está vacío. En la Mini, la cola de reordenación
sólo incluye los grupos administrados y se omite si supera 64 tareas. El vigía
hace un GET a `/api/rack.json` y valida cada host por separado. Una lectura
fallida, parcial en el control o la cola, o con más de dos minutos de antigüedad
conserva los episodios del host. Tras más de 30 minutos sin una lectura válida,
avisa una vez por interrupción. Si el panel deja de responder, las lecturas de
ambos hosts se consideran fallidas.

El vigía también avisa en la primera pasada que detecta menos de 25 GiB libres
(26 843 545 600 bytes) en el disco de la Mini. El aviso muestra el espacio
libre y el umbral en GiB. Mientras una lectura válida siga por debajo del
umbral, el aviso se repite en la primera pasada tras cumplirse 30 minutos
desde la última entrega. Una lectura válida en el umbral o por encima cierra
el episodio. Si falta una lectura válida de disco, el episodio conserva su
estado.

La MacBook sólo puede detectar cambios entre observaciones válidas. Si un
grupo se abre y se vuelve a cerrar entre ellas, el vigía conserva el episodio.

## Actualización en la MacBook

Si el LaunchAgent ya está instalado, reemplaza `aviso.py` desde un checkout
actualizado. En la siguiente pasada el agente lee el código nuevo y conserva
los episodios guardados en `estado.json`.

```sh
cp fleet/pueue-aviso/aviso.py "$HOME/.local/lib/pao-aviso-fila/"
```

## Instalación manual en la MacBook

Desde la raíz de un checkout actualizado, crea los directorios y copia el vigía.

```sh
mkdir -p "$HOME/.local/lib/pao-aviso-fila" "$HOME/.config/pao" "$HOME/Library/LaunchAgents"
mkdir -m 700 -p "$HOME/.local/state/pao/aviso-fila-cerrada"
cp fleet/pueue-aviso/aviso.py "$HOME/.local/lib/pao-aviso-fila/"
```

Guarda este JSON en `~/.config/pao/aviso-fila-cerrada.json`, con la dirección
privada que ya usas para abrir el rack y la ruta `/api/rack.json`. Puedes omitir
`minutos` para usar 30 o indicar un entero positivo para los grupos cerrados,
los fallos de lectura y el intervalo entre avisos de disco. La clave opcional
`disco_minimo_bytes` acepta un entero positivo en bytes y por omisión vale
`26843545600`. El vigía lee la configuración en cada pasada.

```json
{
  "rack_url": "http://direccion-privada-del-rack:8788/api/rack.json",
  "minutos": 30,
  "disco_minimo_bytes": 26843545600
}
```

Desde la sesión gráfica, copia, valida y carga el LaunchAgent.

```sh
agente="$HOME/Library/LaunchAgents/local.pao.aviso-fila-cerrada.plist"
cp fleet/pueue-aviso/launchagents/local.pao.aviso-fila-cerrada.plist "$agente"
plutil -lint "$agente"
launchctl bootstrap "gui/$(id -u)" "$agente"
```

El agente guarda `estado.json` y `launchd.err.log` en
`~/.local/state/pao/aviso-fila-cerrada/`. Para detenerlo, usa
`launchctl bootout "gui/$(id -u)" "$agente"`. macOS controla la visibilidad
de los avisos que envía `osascript`.

Ejecuta las pruebas dirigidas en el desk o la Mini con `remote-run`.

```sh
python3 -I -m unittest discover -s fleet/pueue-aviso/tests -v
```
