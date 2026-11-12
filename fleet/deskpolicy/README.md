# deskpolicy retirado

El desk toma turno con [Pueue](../pueue-desk/). El motor, las pruebas de
admisión y su evidencia anterior se retiraron. Queda sólo la respuesta
terminal y su instalación, para impedir que una rama vieja abra otra fila.

**Integrar y desplegar sólo en el corte coordinado**, con Hasito, Jale y
apotheca ya migrados y las cargas anteriores drenadas. No ejecutar durante
el mantenimiento ni antes de instalar Pueue en el desk.

Desde la MacBook:

```sh
fleet/deskpolicy/deploy-desk.sh --seco
fleet/deskpolicy/deploy-desk.sh
```

`--seco` sólo imprime el programa remoto; no abre SSH ni escribe archivos.
`withdraw-desk.sh` es otro nombre del mismo retiro. Ambos son idempotentes.
Por `ssh desk`, descubre unidades systemd de usuario y sistema y comprueba
`sudo -n true` si hay unidades del sistema, antes de cambiar la CLI o detener
servicios. Prepara ambos archivos de respuesta para detectar errores de
permisos o rutas antes del reemplazo. Instala el rechazo antes de detener nada.

El drenado lee las claims de `/tmp/sen-test-slots/pao-wait/` bajo el mutex
antiguo, además de los procesos `ejecutar` y `admitir` que pudieron cargar
la CLI antes del reemplazo. Si queda una admisión viva, devuelve **2**,
informa el total y los tipos (`jale/chain`, `hasito/script`, etc.) y conserva
writer y unidades sin enviar señales. Repite el retiro después, o usa
`--esperar N` para esperar hasta N segundos. El vencimiento también devuelve 2.

Una claim abierta se considera abandonada sólo si su lock de vida está libre
y todas las sesiones del dueño y sus keepers están vacías, igual que en
`DeskPolicy.reconcile_claim` antiguo. Que muera el dueño no basta si quedan
hijos o un keeper. Los errores de lectura o validación abortan el retiro.
La consulta no cambia claims ni recibos históricos.

La respuesta instalada conserva una sola excepción: **`cerrar` con sus
argumentos** delega a `DeskPolicy.close` de las bibliotecas históricas ya
instaladas. Mantiene las validaciones de token, identidad, mutex, lock de vida
y sesiones vacías; permite a Jale acreditar `script_exit_code` al terminar
durante el drenado. No copia ni ejecuta la CLI antigua, ni habilita admisiones.
El `ejecutar` viejo cierra desde su propio proceso. Las bibliotecas antiguas
siguen intactas; el writer se conserva hasta completar el drenado.

Con el desk drenado, detiene y deshabilita activadores, writer y guardias.
Sólo termina procesos sin unidad identificados como `publicar` de la CLI
instalada y sus hijos, comprobando la identidad PID antes de señalar. No
selecciona procesos por una mención de «deskpolicy» en sus argumentos.
Un fallo posterior a instalar el rechazo lo conserva y devuelve error;
el operador debe resolver el fallo y repetir el retiro para completarlo.

Instala atómicamente la respuesta en la ruta usada por los clientes:
`~/.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py`, y en
`~/.local/bin/pao-deskpolicy`. La ruta base sale de la cuenta remota, no de
`HOME`. Invoca la CLI instalada con varios subcomandos y exige stderr exacto,
stdout vacío y código 64. No borra `pao-wait/`, recibos, locks, claims,
configuración ni bibliotecas históricas del desk. No instala nada local.
Un error del retiro se devuelve al operador; no acredita un corte completo.

La fecha fija de la respuesta es `2026-11-12`, fecha del contrato de retiro.
Salvo el cierre acreditado anterior, incluso `--help` y operaciones desconocidas
fallan sin analizar argumentos. Un cierre inválido nunca acredita éxito.

## Lo que ven las sesiones viejas

**El corte debe anunciarse a las sesiones** con la instrucción: «Actualiza tu
rama a origin/main y usa el lanzador del repositorio». PAO no puede cambiar la
terminal ni los códigos que los clientes antiguos transforman:

- Hasito viejo muestra `No se pudo consultar politica estado: código 64`;
  la instrucción queda en `politica-1.stderr` y `lanzador.log` de la evidencia.
  Con `--lock` puede esperar antes en la fila local de sesiones viejas; cada
  sesión falla al llegar a la consulta y esa fila se drena.
- Jale viejo muestra `EXIT=75` en aproximadamente 1 s. El mensaje de retiro
  queda en el log de la corrida, junto con `no free slot after 90 min`, aunque
  no haya esperado 90 min. Su cliente convierte el 64 en 75; la CLI de PAO no
  puede impedirlo. La migración de Jale elimina este camino.

## Por qué la CLI devuelve 64

Clientes consultados con `git show origin/main:RUTA` el 2026-11-12:

- Hasito (`d0bccf9cb60902301bb6c9bcdf16efdc1f6d2c43`),
  `scripts/mini/remote-run:96-100`: `politica` hace `exec` de Python con la
  CLI instalada y propaga el código. La ruta está en las líneas 68-70.
  `scripts/mini/desk.py:366-375` convierte cualquier código no cero en
  `DeskFailure`; las líneas 472-479 sólo permiten reintentar errores 75 con
  un recibo válido. Las líneas 298-305 tratan 255 como transporte y sólo
  reintentan 75 por mutex; 64 es fallo definitivo.
- Jale (`274e9cf4425f85ec4ce558b51a32a9e2e92dbc6c`),
  `scripts/test-hosts/run-suite.sh:504-508` rechaza cualquier estado no cero;
  `750-758` retorna de inmediato si falla esa consulta, antes de esperar.
  `704-725` sólo admite con recibo `admitted` y código 0; 75 puede significar
  denegación con espera. Se elige 64 para no emitir esa señal ni un éxito.
  Jale convierte el error de estado a 75 al salir, pero su bucle interno no
  reintenta la consulta fallida. No se puede cambiar esa conversión desde la CLI.

## Verificación sin desk

```sh
~/.local/bin/remote-run start --name dirigida-u16-r3 --no-git -- 'python3 -I -m unittest discover -s fleet/deskpolicy/tests -v && shellcheck --severity=warning fleet/deskpolicy/*-desk.sh'
~/.local/bin/remote-run wait ID
```

Las pruebas ejecutan el transporte con SSH falso y systemd simulado, comprueban
dos retiros consecutivos, preservación de registros y fallos. Los procesos se
prueban con dobles que incluyen hijos y reutilización de PID. CI también
ejecuta estas pruebas y ShellCheck. No acreditan un despliegue real.
