# Vigilante externo de Pueue

Libera lugares retenidos por tareas manuales colgadas en la Mini y el desk.
Sólo evalúa tareas `Running`. No toca tareas con lanzador ni tareas DUDOSAS.
No toca pausas, cuotas, la guardia de carga, rack ni el daemon `pueued`.
No cubre procesos que corren fuera de Pueue.

## Identidad y límites

Reconoce tres reglas, desde los campos de la cola y sin abrir archivos:

- Bodega: etiqueta exacta `apotheca-remote-run`.
- Hasito/remote-run: ruta `~/remote-run/runs/AAAAMMDD-HHMMSS-hex6`.
- Jale: comando exacto `exec bash ~/.local/state/jale/pueue/job-XXXXXXXX/task.sh`,
  con ruta absoluta al home y ocho caracteres alfanuméricos.

La franja DUDOSA protege formas parecidas. Incluye rutas bajo
`~/remote-run/runs/` con otro nombre, comandos que contienen
`/remote-run/runs/` o `pueue.py exec`, comandos `exec bash …/job-*/task.sh`
con otro padre y etiquetas que contienen `remote-run`. Se cuentan en la
bitácora. Una persona debe revisarlas. Todo lo demás es MANUAL.

| Grupo | Tope en minutos |
| --- | ---: |
| rapidas | 10 |
| suites, hasito, otros, jale | 45 |
| pesadas | 150 |
| default y cualquier otro | 45 |

El silencio permitido es 15 minutos. La etiqueta puede reemplazar ambos
límites al encolar una tarea manual:

```sh
pueue add -g pesadas --label 'restaurar tope=120 silencio=40' -- bash restaurar.sh
```

Cada declaración debe ser una palabra `tope=N` o `silencio=N`, con N de
1 a 150. Puede bajar el límite. En `rapidas`, el tope nunca pasa de 10.
Una declaración repetida, mal formada o fuera de rango se ignora para ese
límite. Se conserva el valor del grupo y se registra un `aviso`.
Las corridas de más de 150 minutos deben usar un lanzador.

## Reloj y corte

Cada minuto lee `pueue status --json` y sólo hace `stat` del log de Pueue.
El tiempo corre desde `max(start, reanudado_en)`. El silencio corre desde
el más reciente entre esa base y el `mtime` del log. Sin log sólo aplica
el tope. Sin inicio válido, o con inicio más de 60 s futuro, registra
`indeterminada` y no corta.

`pulso.json` guarda `ultima` y `reanudado_en`. Si falta, está corrupto,
el reloj retrocede o pasan más de 180 s entre lecturas, registra `hueco`
y abre una ventana completa nueva. La primera pasada también lo hace.
No hay estado por tarea. El candado `pasada.lock` impide pasadas cruzadas;
una segunda registra `omitida` y sale con 0.

En modo `informe`, registra `cortaria` y no envía señales ni añade marcas.
En modo `cortar`, relee la cola. Exige el mismo `id|created_at|start`,
estado `Running` y clase MANUAL antes de cada señal. Envía
`pueue kill -s SIGTERM ID`, sondea durante 20 s y después usa
`pueue kill ID`. Espera hasta 10 s más. Si persiste, registra `persiste`
y lo reintenta en la siguiente pasada. Una identidad, clase o estado
distintos producen `cambio` y detienen el corte.

Sólo después de observar `Done` de esa huella añade al log:

```text
PAO_VIGILANTE_CORTE motivo=tope limite_min=45.0 grupo=jale transcurrido_min=46.0 sin_salida_min=0.0
```

La marca queda visible con `pueue log ID`. Si no puede escribirla, registra
el error sin anular el corte. Nunca escribe en un log de una tarea viva.

La bitácora JSONL UTC está en
`~/.local/state/pao/pueue-vigilante/vigilante.log`. Rota a `.1` al alcanzar
1 MiB. Guarda huella, grupo, etiqueta, ruta, programa, motivo y medidas.
No copia el comando completo ni el entorno. `fin_corte` indica
`terminada`, `forzada`, `persiste` o `cambio`. `pasada` sólo se registra
si hubo tareas manuales, DUDOSAS o ilegibles. `bitacora.lock` serializa
las escrituras y la rotación, incluidas las pasadas omitidas.

## Instalación desde la MacBook

Hay una sola entrada para ambos hosts. Usa sólo biblioteca estándar y
envía una carga autónoma con `FILES` por
`ssh -o BatchMode=yes -o ControlPath=none HOST /usr/bin/python3 -I -`.
No requiere checkout remoto. No instala Pueue.

Desde la raíz de PAO, comprobar el plan y el censo sin escribir:

```sh
python3 -I fleet/pueue-vigilante/instalar.py instalar --host mini --modo informe --seco
python3 -I fleet/pueue-vigilante/instalar.py instalar --host desk --modo informe --seco
```

El ensayo sí contacta el host. Hace las comprobaciones previas y muestra
los conteos por clase y regla. No carga unidades ni escribe estado.
`instalar` sin `--modo` sale con 2.

Instalar en informe:

```sh
python3 -I fleet/pueue-vigilante/instalar.py instalar --host mini --modo informe
python3 -I fleet/pueue-vigilante/instalar.py instalar --host desk --modo informe
```

Antes de escribir exige el daemon activo y una cola válida sin tareas
ilegibles. La Mini comprueba `launchctl print gui/$UID/sh.brew.pueue` y
usa `/opt/homebrew/bin/pueue`. El desk comprueba `pueued.service` y usa
`~/.local/bin/pueue`. Los respaldos `.bak-<sello>` comparten sello.
Reinstalar sin cambios no crea otro respaldo. La escritura es atómica.

El código queda en `~/.local/lib/pao-pueue-vigilante/vigilante.py`.
El estado queda en `~/.local/state/pao/pueue-vigilante/` en ambos hosts.
La Mini usa `local.pao.pueue-vigilante.plist`, con `StartInterval 60`.
Sus salidas van a `launchd.out.log` y `launchd.err.log` en el estado.
El desk usa un servicio de usuario `oneshot` y un timer de 60 s.
El servicio tiene `After=pueued.service`, sin `Requires=`.

## Arranque y humo supervisado

Instalar primero en `informe`. Hacer el humo en cada host, con su binario
de Pueue. Estos comandos son pasos de operación; no forman parte del test:

```sh
pueue add -g default --label 'pao-humo tope=1' -- sleep 600
```

A los 2 o 3 minutos debe aparecer `cortaria`. Cancelar esa tarea a mano.
Revisar el censo, las tareas DUDOSAS y las filas `cortaria` y `hueco`.
Para leer el censo desde el propio host:

```sh
# Mini
/usr/bin/python3 -I ~/.local/lib/pao-pueue-vigilante/vigilante.py censo --host mini --pueue /opt/homebrew/bin/pueue
# Desk
/usr/bin/python3 -I ~/.local/lib/pao-pueue-vigilante/vigilante.py censo --host desk --pueue ~/.local/bin/pueue
```

Con la revisión resuelta, pasar a cortar y repetir el humo:

```sh
python3 -I fleet/pueue-vigilante/instalar.py instalar --host mini --modo cortar
python3 -I fleet/pueue-vigilante/instalar.py instalar --host desk --modo cortar
```

En cada host, encolar primero el caso que ignora SIGTERM:

```sh
pueue add -g default --label 'pao-humo tope=1' -- bash -c 'trap "" TERM; sleep 600'
```

Se espera `fin_corte` con `desenlace=forzada`, la marca al final del log
y ningún proceso de ese humo vivo. Identificar el PID y sus hijos antes
del humo; no terminar procesos ajenos por nombre. Repetir con `sleep 600`
sin trap y comprobar `desenlace=terminada`.

El humo mide qué alcanza SIGTERM en Pueue 4.0.4: hijo o grupo de procesos.
Queda pendiente medirlo en el host. Allí también hay que consultar
`pueue edit --help` para confirmar las restricciones sobre tareas vivas y
anotar los dígitos de fracción que escribe el desk. El parser acepta `Z`,
offsets y de uno a nueve decimales. CI imprime la versión del Python de
Apple antes de ejecutar la suite con él. No se fija una espera de 24 h.

## Volver y retirar

Desde la MacBook, elegir el host. Ambos admiten `--seco`:

```sh
python3 -I fleet/pueue-vigilante/instalar.py volver --host mini --seco
python3 -I fleet/pueue-vigilante/instalar.py volver --host mini
python3 -I fleet/pueue-vigilante/instalar.py retirar --host desk --seco
python3 -I fleet/pueue-vigilante/instalar.py retirar --host desk
```

`volver` restaura el juego completo de respaldos más reciente y recarga.
Antes copia los archivos que reemplaza a `.antes-volver-<sello>`.
Rechaza juegos incompletos. `retirar` detiene sólo el trabajo del vigilante
y renombra el código y las unidades a `.retirado-<sello>`. Conserva el
pulso, los candados y la bitácora. Repetir el retiro no cambia archivos.
Si una carga falla después de escribir, revisar la salida y reinstalar o
volver; no se modifica ni reinicia el daemon de Pueue.

## Pruebas dirigidas

Sólo en el desk o la Mini. Usan dobles privados y no el daemon real.
La suite inyecta gracia de 0,2 s y reloj. Debe tardar menos de dos minutos.
Desde la raíz del repo:

```sh
# Desk
python3 -I -m unittest discover -s fleet/pueue-vigilante/tests -v
# Mini
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-vigilante/tests -v
```

Para la RED, `PAO_VIGILANTE_SOURCE` puede apuntar al directorio del
esqueleto con los dos módulos. El fake y las unidades siguen saliendo del
checkout de las pruebas. La paridad sólo carga `fleet/rack/snapshot.py`
por ruta, con `importlib`, y comprueba que toda tarea que rack reconoce
queda fuera de MANUAL.
