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
Por `ssh desk`, el programa descubre unidades systemd de usuario y sistema
por nombre o contenido que mencione deskpolicy, detiene sus activadores,
writer y guardias, deshabilita las unidades habilitadas y verifica su estado.
Las unidades del sistema requieren `sudo -n`; un permiso insuficiente aborta.
También termina procesos de deskpolicy y sus hijos, incluido el writer
desacoplado que no tenga unidad, sin reutilizar identidades PID antiguas.

Instala atómicamente la respuesta en la ruta usada por los clientes:
`~/.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py`, y en
`~/.local/bin/pao-deskpolicy`. La ruta base sale de la cuenta remota, no de
`HOME`. Invoca la CLI instalada con varios subcomandos y exige stderr exacto,
stdout vacío y código 64. No borra `pao-wait/`, recibos, locks, claims,
configuración ni bibliotecas históricas del desk. No instala nada local.
Un error del retiro se devuelve al operador; no acredita un corte completo.

La fecha fija de la respuesta es `2026-11-12`, fecha del contrato de retiro.
Incluso `--help` y operaciones desconocidas fallan sin analizar argumentos.

## Por qué devuelve 64

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
~/.local/bin/remote-run start --name dirigida-u16-retiro-deskpolicy --no-git -- 'python3 -I -m unittest discover -s fleet/deskpolicy/tests -v && shellcheck --severity=warning fleet/deskpolicy/*-desk.sh'
~/.local/bin/remote-run wait ID
```

Las pruebas ejecutan el transporte con SSH falso y systemd simulado, comprueban
dos retiros consecutivos, preservación de registros y fallos. Los procesos se
prueban con dobles que incluyen hijos y reutilización de PID. CI también
ejecuta estas pruebas y ShellCheck. No acreditan un despliegue real.
