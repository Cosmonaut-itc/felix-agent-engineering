# Lectura nativa del snapshot del desk

Estado: revisión documental preestable r3. No activa transporte ni declara APTO o estabilización.

Revisión documental preestable de 2026-10-24 del contrato `fleet/desk-snapshot-read@1`, fijado por Fleet para SEN-3916, SEN-3823 y SEN-3830. El literal del contrato se mantiene sin cambios. No declara APTO, certificación ni activación. Describe el comando que debe certificarse. No implementa `world.health()`; el hostgate de main `ee8bb5aecf7425eaeb7dcab9562bab8f85a52391` sigue en sombra con un doble. SEN-3900 no implementa este transporte.

## Productor y archivo

Productor instalado de Desktop `a6c1010f`, PR 16, tarea `\PAO desktop vigilante`. La ruta Windows acordada es `C:\Users\felix\AppData\Local\pao\desktop-vigilante\state\ultimo.json`. La tarea instalada aplica el override real `state`; no se usa el default de un script.

La ruta de Git Bash es exactamente `/c/Users/felix/AppData/Local/pao/desktop-vigilante/state/ultimo.json`. No es parámetro del trabajo ni se obtiene de su entorno. El archivo contiene un único objeto JSON UTF-8 sin BOM. Puede no terminar en newline. Es compacto y no es JSONL. Su esquema es `desktop-vigilante/muestra@1`; los campos superiores acordados son `schema`, `ts`, `host`, `boot`, `status`, `metrics`, `volumes`, `guestRuntime`, `persist` y `errors`. Se toleran ampliaciones aditivas conforme al lector puro existente.

Desktop publica mediante temporal GUID en el mismo directorio y reemplazo atómico `File.Replace` o `File.Move`, sin promesa de fsync. Un fallo puede conservar la muestra anterior. PT1M, IgnoreNew y StartWhenAvailable no garantizan una publicación cada minuto. Una actualización de persist puede conservar el mismo ts. El tamaño observado de 2182 bytes no es una cota del productor.

## Comando que certifica Desktop

El único string remoto enviado como argumento de SSH es:

```sh
exec /usr/bin/head -c 65537 -- '/c/Users/felix/AppData/Local/pao/desktop-vigilante/state/ultimo.json'
```

La invocación nativa desde MacBook es este argv, representado en shell sólo para poder leerlo:

```sh
/usr/bin/ssh -T -n -o BatchMode=yes -o ProxyCommand=none -o ProxyJump=none -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=5 -o ConnectionAttempts=1 Felix@felix-desktop "exec /usr/bin/head -c 65537 -- '/c/Users/felix/AppData/Local/pao/desktop-vigilante/state/ultimo.json'"
```

Fleet construye un argv local literal y un único argumento remoto con esos bytes. No usa shell local. El DefaultShell nativo de sshd es Git Bash; su manejo de CommandOption, quoting y wrappers forma parte del gate que Desktop mide. El builtin exec solicita reemplazar el shell por head; no se presume que esto acredite cierre de procesos en Windows.

Sólo se lee ese archivo una vez por evaluación. No hay pipelines, reintentos, lecturas auxiliares, cat alternativo, stat, wc, scripts, source, eval, PowerShell, cmd, CIM, WMI ni WSL en el comando. El alias desk no se usa. La puerta no arranca el monitor ni WSL si no están disponibles. La instalación de Git debe aportar `/usr/bin/head` con `-c` y `--`; si H2 no confirma esa interfaz exacta, no hay fallback y esta versión no pasa el gate.

## Límites y resultado de la lectura

El contenido aceptado tiene como máximo **65 536 bytes**. head solicita **65 537 bytes** para detectar el exceso con un byte centinela. El consumidor nunca intenta parsear un prefijo truncado como muestra válida. Una salida de 65 537 bytes se rechaza completa aunque SSH y head salgan con cero. El lector conserva como máximo esos bytes de stdout y 4096 bytes de stderr, con drenaje simultáneo acotado. Stderr no vacío por sí solo no invalida una salida cero con JSON válido; se conserva sólo como diagnóstico acotado y saneado, descartando el exceso sin crecer. No registra contenido crudo de la muestra ni stderr sin sanear en recibos públicos.

El presupuesto para aceptar una respuesta completa es **cinco segundos de reloj monotónico desde antes del spawn de SSH**. `ConnectTimeout=5` sólo acota la conexión y no sustituye este plazo. Antes del spawn, el controlador bloquea durablemente ese endpoint bajo su exclusión. Si existe una limpieza no confirmada anterior, no hace spawn y publica no sano `health-invalid` con motivo `snapshot-cleanup-unconfirmed`. El estado durable es común al controlador y sobrevive a la pérdida del proceso evaluador; no se fija aquí un servicio ni un formato de almacenamiento. Al vencer, el timeout es irrevocable. El cliente envía SIGTERM sólo a su propio proceso SSH local y espera 0,25 segundos para recogerlo. Si sigue vivo, envía SIGKILL sólo a ese hijo y espera 0,75 segundos más para recogerlo. La limpieza añade como máximo un segundo al plazo de respuesta. Si el hijo no queda recogido, el cliente conserva durablemente el bloqueo, además de su identidad o handle, hasta reconciliación positiva de su SSH propio. La marca se limpia al recogerlo positivamente. No espera indefinidamente, no reintenta y no espera una respuesta remota tardía. Desktop certifica qué sucede con `head`, Bash y los wrappers remotos al perder el cliente. Hasta esa acreditación no se habilita la puerta. La limpieza de transporte no prueba cierre remoto ni terminalidad de cargas y no libera reservas. Los plazos monotónicos son presupuestos de control, no garantías de planificación en tiempo real.

| Observación | Resultado de la puerta |
|---|---|
| Plazo de cinco segundos vencido | No sano, `health-invalid`, causa `snapshot-read-timeout`. |
| Más de 65 536 bytes o salida que viola la cota | No sano, `health-invalid`, causa `snapshot-too-large`. No se reduce la muestra para admitirla. |
| Archivo ausente, permisos, comando no disponible, SSH no cero o error de lectura | No sano, `health-invalid`, causa `snapshot-read-failed`. No se usa stdout parcial aunque parezca JSON. |
| Vacío, UTF-8 inválido, BOM, JSON incompleto, duplicados, no finitos, prefijo o sufijo ajeno al objeto JSON, basura final, JSONL o raíz distinta de objeto | No sano, `health-invalid`, causa `snapshot-invalid-json`. La ausencia de newline final sí es válida. |
| Una muestra completa no supera evaluar o trae schema desconocido | No sano; motivos del adaptador existente, sin convertir datos desconocidos en cero. |
| Edad desde ts fuera de [-60, 180] segundos | No sano, `health-invalid`, motivo `vigencia`. No se sustituye ts por mtime, frecuencia ni fecha de persist. |
| Sin testigo independiente del boot Windows actual | No sano, `boot-unverified`. El boot del propio snapshot no es ese testigo. SEN-3853 sigue abierto. |

El consumidor publica una sola causa de transporte bajo `health-invalid`, con esta precedencia: limpieza anterior pendiente (sin spawn), timeout, exceso, lectura fallida, JSON inválido y, por último, evaluación de salud o boot. Las razones independientes `default-shell-unverified` y `boot-unverified` que aporte el adaptador se conservan y no se suprimen por elegir una causa de transporte. Los buffers permanecen acotados a 65 536 bytes de stdout más el byte centinela y 4096 bytes de stderr. Al alcanzar sus cotas, el consumidor sigue drenando y descarta el exceso hasta que termine el proceso o venza el plazo.

Tras una lectura completa con salida cero y JSON válido, la interfaz sigue siendo `fleet/hostgate/salud/lector_desk.py:evaluar(muestra, ahora, boot_actual)`, función pura sin E/S. El lector adoptado es el blob `1ef0465d847836129dc155a80bf34fac8e169a4a`. El adaptador convierte sus motivos según el contrato hostgate existente; no sustituye ni duplica las reglas de sano.

`ahora` es la hora UTC del lector al evaluar tras la lectura. `boot_actual` procede de una interfaz independiente aceptada de Desktop, todavía pendiente. La ventana de ts es inclusiva, `-60 <= ahora - ts <= 180`. Los fallos anteriores dejan desk no sano; un boot no verificable tampoco permite elegirlo degradado. Ningún fallo cambia drenado, libera capacidad ni descarta duda observada. Los motivos nuevos propuestos aquí son detalles bajo `health-invalid`, no nuevos reason codes públicos del protocolo host-reservation.

## Alcance de H2 y H3

H2 y H3 deben certificar este argv y string remoto exactos con Git Bash, su versión instalada y el head documental recibido. Deben acreditar la cadena de identidades, quoting, lectura normal y cierre de Bash, `head` y wrappers al terminar o perder el cliente. Incluyen ausencia del archivo, lectura fallida, exceso, salida sin newline y timeout o pérdida separados. H2 y H3 siguen pendientes, incluida la custodia. No prueban salud real, testigo de boot, autorización de cargas ni ausencia de residuos por sólo obtener SSH cero.

Quedan pendientes de verificación, fuera de H2/H3/Estado #5, el origen y la configuración efectiva SSH, incluidos el binario y versión, archivos fuente e `Include`, identidad, agente y archivos `known-hosts`; la exclusión de `Match exec`, `LocalCommand` y otros comandos o proxies locales; los perfiles y modo login de Bash, `BASH_ENV`, `ENV` y el entorno heredado de sshd; la ausencia de bytes antes de `head` y de procesos fuera de la cadena; y una lectura concurrente con `File.Replace` que compruebe que la lectura no impide la publicación de Desktop. Estas verificaciones, junto con H2/H3 y DefaultShell, son prerrequisitos antes de habilitar la lectura. Permanecen fuera de H2/H3/Estado #5 y no amplían esas ventanas; PAO coordina cuándo y cómo hacerlas y pedirá otra ventana a Felix si hace falta. Sólo el dueño ejecutará `ssh -G` durante una certificación autorizada y después de inspeccionar las fuentes de configuración. Fleet no abre conexiones ni consulta hosts aquí. Un cambio relevante del origen o de la configuración SSH efectiva obliga a revisar la evidencia, además de cualquier cambio de ruta, comando, shell o límites.

La afirmación "No usa shell local" describe la construcción literal del argv; no certifica el comportamiento interno de una configuración no revisada. Los requisitos pendientes no prueban salud real, testigo de boot, autorización de cargas ni ausencia de residuos por sólo obtener SSH cero.

Las regresiones requeridas para las decisiones nuevas distinguen: salida cero con JSON válido y stderr no vacío (aceptable, con stderr sólo diagnóstico acotado/saneado); endpoint bloqueado tras pérdida de recogida, incluido reinicio del evaluador, que impide spawn hasta reconciliación positiva; bloqueo durable escrito antes del spawn y limpiado tras recogida positiva; una sola causa de transporte con razones independientes de shell/boot preservadas; y cambios de origen/configuración SSH que exigen revisión de evidencia. Son requisitos, no pruebas afirmadas como escritas o ejecutadas.

Un cambio de ruta, comando, shell o límites requiere una revisión explícita del contrato y de su certificación. Este documento se redactó sólo desde código y datos de contrato comunicados por PAO y Desktop. Fleet no abrió conexiones al desk ni leyó el snapshot real.
