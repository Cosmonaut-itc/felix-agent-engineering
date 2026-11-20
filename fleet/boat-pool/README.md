# Pool de VMs de Boat

`boat-pool` concede lugares en VMs preparadas por los lanzadores de los
repositorios. Reanuda las VMs cuando hace falta y un LaunchAgent las apaga
cuando llevan el tiempo de inactividad configurado.

## Instalación en la MacBook

Copia [pool.example.json](pool.example.json) a
`~/.config/boat-pool/pool.json` y completa la organización, las VMs y
`<servicio-del-llavero>`. En la lista obligatoria `prohibidos`, pon los IDs
reservados del inventario privado. Los bloqueos permanentes se escriben
como cadena y los temporales como un objeto con `id` y `hasta` en UTC.
El instalador copia el programa a `~/.local/lib/boat-pool`, lo enlaza en
`~/.local/bin/boat-pool` y carga el LaunchAgent en la sesión gráfica.

```sh
fleet/boat-pool/install.sh
```

El LaunchAgent ejecuta `vigilar` al cargar y cada cinco minutos. Cada VM
puede indicar su propio `inactividad_min` o usar el valor global. Con
`inactividad_min: null`, `vigilar` conserva la VM encendida.
El estado y los avisos quedan en `~/.local/state/boat-pool`. Para los comandos
interactivos también se admiten `XDG_STATE_HOME` y `XDG_CONFIG_HOME`.
El LaunchAgent usa los valores del entorno de launchd.

## Operación

- `pedir --repo R --corrida ID --clase C [--prioridad P] [--esperar S] [--ttl S]`
  devuelve un JSON con el arriendo, nombre de VM, ID en Boat y vencimiento.
  Los valores predeterminados son prioridad `otra`, espera de cero segundos
  para un lugar y arriendo de 900 segundos. La espera admite de 0 a 7200 s
  y el arriendo de 300 a 3600 s. Una reanudación tiene su propio plazo.
- `pedir --repo R --corrida ID --clase preparar --vm NOMBRE` reserva toda
  la VM para el repositorio dueño aunque el registro indique `apta: false`.
  Mientras el turno espera, la VM deja de otorgar lugares. Una vez que los
  arriendos vigentes terminan, `preparar` tiene prioridad sobre `tren`,
  `suite` y `otra`.
- `latido ARRIENDO [--ttl S]` renueva el arriendo desde el momento actual.
  El lanzador lo llama cada `ttl/3`. Si sale 69, el lanzador termina la
  corrida e informa la pérdida del arriendo.
- `soltar ARRIENDO` libera el lugar y se puede repetir.
- `estado [--json]` muestra VMs, arriendos, lugares y puertas de arranque.
- `encender NOMBRE|--todas` y `apagar NOMBRE|--todas` actúan sobre VMs
  declaradas y registradas. Una VM con arriendos vivos se protege del apagado.
- Los lanzadores piden arriendos desde la MacBook. En otros hosts, si el
  comando no está instalado, si falta `pool.json` o ante otra salida 75,
  usan su host habitual. La primera línea de stderr de una salida 75 es
  `boat-pool: <causa>: <detalle>`. Las causas son `sin_vm`, `sin_lugar`,
  `puerta`, `api` y `apagado`.
- `reporte [--dia AAAA-MM-DD]` escribe una fila TSV por día UTC. Las
  columnas son fecha, horas `default`, `large` y `xlarge`, USD estimados,
  arranques del pool, corridas distintas concedidas, horas facturables
  (ponderadas por tamaño) y USD de la organización. Las últimas dos
  columnas suman el consumo diario del inventario de Boat. Si falta
  consumo para ese día, muestran `NA` y el motivo sale en stderr. Boat
  informa el consumo de VMs borradas como un total del mes corriente.
  Las horas del pool se estiman desde el primer estado facturable
  observado o, si la VM ya estaba encendida, desde su fecha de preparación.

Cada lanzador escribe de forma atómica `vms/<nombre>/registro.json` en el
directorio de estado al acabar de preparar. El registro v1 contiene
`nombre`, `id`, `repo`, `tipo`, `preparada_en`, `revision_runner`,
`cpu_robada_pct` y `apta`. Se coteja con la declaración y con el nombre e
ID devueltos por Boat. Cada lanzador decide cómo tratar una revisión del
runner distinta de la que necesita.

`vigilar` amplía a siete días el TTL de Boat si `archiveAfter` queda a menos
de 48 horas. Si una VM lleva 12 días detenida según `stoppedAt`, el vigía
avisa que su disco se acerca al límite de retención de 14 días.

Si la llave tiene un alcance que impide listar llaves, `vigilar` registra
que no pudo comprobar su vencimiento. Los avisos se escriben en `avisos.log`
y se notifican mediante macOS.

## Pruebas

Ejecuta la suite dirigida en la Mini con el Python de macOS, 3.9 o superior.

```sh
remote-run start --sha <HEAD40> \
  --name dirigida-sen-6820 --script fleet/boat-pool/tests/remoto.sh
remote-run wait -q <id>
```
