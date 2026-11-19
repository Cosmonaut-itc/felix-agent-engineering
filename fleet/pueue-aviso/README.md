# Aviso de grupos cerrados con fila

En la MacBook, el LaunchAgent consulta cada tres minutos la vista privada del
rack. Avisa cuando observa `manual` o `ambiguous` y al menos una tarea
`Queued` en el mismo grupo durante más de 30 minutos. Cuenta desde la primera
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

La MacBook sólo puede detectar cambios entre observaciones válidas. Si un
grupo se abre y se vuelve a cerrar entre ellas, el vigía conserva el episodio.

## Instalación manual en la MacBook

Desde la raíz de un checkout actualizado, crea los directorios y copia el vigía.

```sh
mkdir -p "$HOME/.local/lib/pao-aviso-fila" "$HOME/.config/pao" "$HOME/Library/LaunchAgents"
mkdir -m 700 -p "$HOME/.local/state/pao/aviso-fila-cerrada"
cp fleet/pueue-aviso/aviso.py "$HOME/.local/lib/pao-aviso-fila/"
```

Guarda este JSON en `~/.config/pao/aviso-fila-cerrada.json`, con la dirección
privada que ya usas para abrir el rack y la ruta `/api/rack.json`. Puedes omitir
`minutos` para usar 30 o indicar un entero positivo que se aplica a los grupos
cerrados y a los fallos de lectura. El vigía lee la configuración en cada pasada.

```json
{
  "rack_url": "http://direccion-privada-del-rack:8788/api/rack.json",
  "minutos": 30
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
