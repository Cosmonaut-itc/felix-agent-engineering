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

Si el tren usa Boat, actualiza ambos desde el checkout instalado bajo el
candado del programa. Antes de ejecutar el comando, asigna a `checkout`
la ruta absoluta del checkout y a `sha` el SHA ya descargado.

```sh
pao-tren candado -- sh -c '
  git -C "$1" merge --ff-only "$2" &&
  "$1/fleet/boat-pool/install.sh"
' sh "$checkout" "$sha"
```

El instalador copia el pool y recarga `vigilar` bajo el candado. El tick
siguiente ya puede usar el pool nuevo. Véase
[Instalar una versión nueva](../tren/README.md#instalar-una-versión-nueva).

El LaunchAgent ejecuta `vigilar` al cargar y cada cinco minutos. Cada VM
puede indicar su propio `inactividad_min` o usar el valor global. Con
`inactividad_min: null`, `vigilar` conserva la VM encendida.
El estado y los avisos quedan en `~/.local/state/boat-pool`. Para los comandos
interactivos también se admiten `XDG_STATE_HOME` y `XDG_CONFIG_HOME`.
El LaunchAgent usa los valores del entorno de launchd.

Cada VM puede declarar `peso_clase` en `pool.json`, por ejemplo `{"contract": 2}`. Cada peso es un entero entre 1 y `lugares` y las clases sin peso declarado ocupan un lugar por arriendo.

## Operación

- `pedir --repo R --corrida ID --clase C [--candidatas A,B] [--prioridad P] [--esperar S] [--ttl S]`
  devuelve un JSON con el arriendo, nombre de VM, ID en Boat y vencimiento.
  `--candidatas A,B` limita la concesión a esas VMs de `pool.json` del mismo
  repositorio y sólo se admite en clases distintas de `preparar`.
  Entre las VMs con cupo, `pedir` pospone las que tienen un apagado pendiente
  o cuyo último evento de energía para el ID vigente es un apagado. Mientras
  sigue una reanudación, da preferencia a las candidatas que ya están listas.
  Elige por menor proporción de lugares ocupados y desempata en el orden de
  `pool.json`. Un diario ilegible deja la VM al final. Si la lectura del diario
  falla al validar la candidata, `pedir` prueba con las demás.
  Cuando el pool confirma que una VM está lista después de un evento
  `apagada` del mismo ID y sin un apagado pendiente, registra `encendida`.
  La VM vuelve al orden por carga.
  Los valores predeterminados son prioridad `otra`, espera de cero segundos
  para un lugar y arriendo de 900 segundos. La espera admite de 0 a 7200 s
  y el arriendo de 300 a 3600 s. Una VM está arrancando cuando Boat la
  muestra en arranque o detenida y su marca en `operacion.json` para ese ID
  registra un `inicio` de hace menos de 420 s. Para marcas anteriores se
  toma `desde` como `inicio`. Cualquier pedido que tenga la VM como candidata
  puede adoptar esa reanudación y deja libre el candado de la VM entre
  consultas. Si a los 90 s del último POST Boat aún la muestra detenida,
  `pedir` puede reenviar el `resume` y renovar `desde`. Los reenvíos
  conservan `inicio`. Si Boat la muestra en arranque, `pedir` sigue
  consultando su estado. Un pedido que sigue una reanudación permite al
  siguiente turno elegible arrancar otra VM detenida. Las concesiones
  cuando una VM queda lista siguen el orden de la fila.
  `pedir` publica el plazo del turno antes de enviar el POST. Mientras alguna
  candidata arranca, el turno puede durar más que `--esperar` y se extiende
  hasta el mayor plazo de esas reanudaciones. Si a los 420 s
  del primer POST Boat sigue sin confirmar que la VM está lista, el pedido
  la rechaza con `Boat no confirmó el cambio de estado`. Si otro proceso
  tiene el candado al vencer el plazo o tras limpiar la marca, un pedido
  que siguió la reanudación tiene hasta 10 s de margen para observar la VM.
  Tras limpiar la marca, el margen se cuenta desde la primera contención
  sin marca. Si el último sondeo encuentra un intento iniciado después
  del vencimiento del que seguía el pedido, rechaza esa candidata.
  Un pedido nuevo puede iniciar otra reanudación de una VM detenida con
  una marca ya vencida cuando se cumplan 90 s desde el último POST. Hasta
  entonces puede seguir sondeando dentro de su `--esperar` y probar
  otras candidatas. Estados como `error` o `cancelled` provocan el rechazo
  inmediato. Si `pedir` no consigue un arriendo, sale con 75.
- `pedir --repo R --corrida ID --clase preparar --vm NOMBRE` reserva toda
  la VM para el repositorio dueño aunque el registro indique `apta: false`.
  Cuando la reserva del tren lo permite, un turno `preparar` impide que la VM
  siga otorgando lugares y tiene prioridad sobre `tren`, `suite` y `otra`
  una vez que terminan los arriendos vigentes.
- `latido ARRIENDO [--ttl S]` renueva el arriendo desde el momento actual.
  El lanzador lo llama cada `ttl/3`. Si sale 69, el lanzador termina la
  corrida e informa la pérdida del arriendo.
- `soltar ARRIENDO` libera el lugar y se puede repetir.
- `reservar --repo R --lote ID --clases C1,C2 --ttl S` anuncia un lote del tren
  al formarlo, antes del CI. Repetirlo con el mismo repo y lote reemplaza
  las clases y renueva el vencimiento. El TTL admite de 300 a 3600 s.
  Mientras viva la reserva, un turno ajeno al tren entra sólo si deja lugar
  para las clases pendientes. Los arriendos vivos del tren cubren sus clases.
  Si la reserva impide la concesión hasta vencer la espera, `pedir` devuelve `sin_lugar`.
- `reservar --repo R --lote ID --clases C1,C2 --vence EPOCH` usa un instante
  Unix en segundos, limitado a 3600 s desde la aplicación. Para una reserva
  del mismo repo y lote ya fijada con `--vence`, Boat conserva el menor
  vencimiento. Si la reserva caduca o se libera, Boat descarta los reintentos
  con `--vence` hasta 3600 s después del vencimiento guardado.
  `--ttl` sigue renovando desde el reloj de Boat.
  Un instante alcanzado libera la reserva del lote y devuelve 0. Usa sólo
  una de las opciones `--vence` y `--ttl`.
- `liberar-reserva --repo R --lote ID` quita la reserva del lote.
  Se puede repetir. Si el tren omite la liberación, la reserva vence sola.
- `estado [--json]` muestra VMs, arriendos, lugares, puertas de arranque y reservas vivas.
- `encender NOMBRE|--todas` y `apagar NOMBRE|--todas` actúan sobre VMs
  declaradas y registradas. `encender` espera hasta 420 s a que Boat confirme
  la reanudación. Una VM con arriendos vivos se protege del apagado.
- Los lanzadores piden arriendos desde la MacBook. En otros hosts, si el
  comando no está instalado, si falta `pool.json` o ante otra salida 75,
  usan su host habitual. La primera línea de stderr de una salida 75 es
  `boat-pool: <causa>: <detalle>`. Las causas son `sin_vm`, `sin_lugar`,
  `puerta`, `api` y `apagado`.
- `reporte [--dia AAAA-MM-DD]` escribe una fila TSV por día UTC, con estas
  columnas en orden.

  1. Fecha UTC.
  2. Horas `default`, `large` y `xlarge` (tres columnas), y USD estimados del
     pool. El reporte lee los diarios y registros vigentes y retirados en
     `vms/`. El periodo de cada ID empieza en el `createdAt` que Boat
     confirma con el mismo nombre e ID. El periodo termina con el marcador
     `registro.retirado-<epoch_ns>.json` que el lanzador deja tras confirmar
     el borrado. Según el contrato v1.6, si faltaba `registro.json`, el
     lanzador escribe el marcador con `id`, `nombre`, `tipo`, `preparada_en`
     igual a la creación local de la VM y `apta: false`. Si el registro sigue
     vigente, el periodo llega hasta ahora.
     Si faltan ambos registros, los días posteriores al último evento del ID
     muestran `NA`. Boat mide el uso en `/sandboxes/{id}/usage`
     desde medianoche UTC hasta la siguiente medianoche o hasta ahora. Las
     horas suman `seconds / billingMultiplier / 3600` según el tamaño actual
     (`sandboxType`). Si el tamaño cambió durante el día, todas sus horas
     se atribuyen al tamaño actual y los USD siguen sumando `dollars` a
     precio de lista. Sin creación acreditada, todos los días del periodo
     muestran `NA` y el próximo reporte reintenta confirmar la creación.
     Un ID desconocido o reservado y una consulta fallida también dan `NA`
     en las cuatro columnas, con el motivo en stderr.
     Un cierre inválido o un evento de actividad sin ID fuera de todo periodo también dan `NA`.
     El cierre requiere 1 s desde la preparación y creación. A menos de 1 s de medianoche UTC, la creación da `NA` en ambos días y el cierre conserva la consulta del día que empieza en esa medianoche.
     Una VM declarada da `NA` desde su creación si el inventario de Boat
     muestra un ID ajeno a sus registros y diario. Si falta `deletedCost`,
     la consulta falla o el inventario sigue cambiando tras el reintento,
     las cuatro columnas del pool muestran `NA` en todos los días.
  3. Arranques del pool y corridas distintas concedidas, según los eventos
     `arranque` y `concedido` del diario. Las corridas se deduplican por
     repositorio e identificador dentro del día.
  4. Horas facturables ponderadas y USD de la organización, según el
     inventario paginado de Boat con `include=cost,deletedCost`. Incluyen
     las VMs reservadas y borradas y repiten el acumulado del mes UTC en
     curso hasta ahora. Para fechas de otro mes o si Boat no confirma el
     total, muestran `NA` y el motivo sale en stderr.

Cada lanzador escribe de forma atómica `vms/<nombre>/registro.json` en el
directorio de estado al acabar de preparar. El registro v1 contiene
`nombre`, `id`, `repo`, `tipo`, `preparada_en`, `revision_runner`,
`cpu_robada_pct` y `apta`. Se coteja con la declaración y con el nombre e
ID devueltos por Boat. Cada lanzador decide cómo tratar una revisión del
runner distinta de la que necesita.

`vigilar` amplía a siete días el TTL de Boat para VMs con `inactividad_min: null`
si `archiveAfter` queda a menos de 48 horas.
Con inactividad configurada, la creación y la reanudación fijan el plazo
de auto-stop de una VM ociosa. `vigilar` conserva ese plazo y apaga la VM
si no puede acotarlo. En el peor caso son unas 2.5 horas por VM con
30 minutos de inactividad.
Si Boat no acreditó que la reanudación dejó la VM sin las
credenciales de su creador, `vigilar` reintenta el apagado pendiente y
omite la renovación del TTL hasta que Boat confirme el apagado. Si una VM
lleva 12 días detenida según `stoppedAt`, el vigía avisa que su disco se
acerca al límite de retención de 14 días.

Si la llave tiene un alcance que impide listar llaves, `vigilar` registra
que no pudo comprobar su vencimiento. Los avisos se escriben en `avisos.log`
y se notifican mediante macOS.

## Pruebas

Ejecuta la suite dirigida en el desk con Python 3.12 y 3.9.

```sh
REMOTE_RUN_HOST=desk remote-run start --carril rapido --sha <HEAD40> \
  --name dirigida-sen-6820 --script fleet/boat-pool/tests/remoto.sh
REMOTE_RUN_HOST=desk remote-run wait -q <id>
```
