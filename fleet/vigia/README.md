# Vigía de Pueue

Python 3.9 y stdlib. Instalar localmente con `./instalar.sh --seco` y
`./instalar.sh`; se conservan las versiones anteriores como `.bak-<fecha>`.

`pao-vigia timbre desk|mini TAREA VISTO PLAZO` espera un cambio (0–900 s).
Usa `-` la primera vez y después el `VIGIA_TOKEN` recibido. Imprime `VIGIA=cambio`,
`plazo` o `sin-vigia`; los dos primeros incluyen token y estado de Pueue.
Argumentos inválidos salen con 2; las respuestas operativas, con 0.

**Regla de los lanzadores:** si llega `VIGIA_TOKEN=`, hacer la consulta propia
ahora, sin dormir antes. Sin token o sin ejecutable, conservar la cadencia
actual. Con cancelación, recuperación o fallo de transporte, omitir el timbre.
El estado recibido es una pista: la consulta propia sigue siendo la autoridad.

Un corredor por host mantiene un SSH con `ControlPath=none`. Envía el código por
stdin: nada se instala ni escribe en los hosts. `flujo` sólo hace stat y
`pueue status --json`, con debounce de 0,3 s, mínimo 1 s entre lecturas y tick de
15 s. El token es SHA256 canónico de grupo, comando, estado y fechas/resultado.

Protocolo SSH por líneas: `S ID`/`U ID`; respuestas `T ID TOKEN ESTADO`, `L` y
`E motivo`. Socket local: `ESPERA 1 ID VISTO PLAZO`; respuesta `cambio TOKEN
ESTADO`, `plazo TOKEN ESTADO`, `sin-vigia` o `error motivo`. El corredor reconecta
con espera 1, 2, 4…60 s y resuscribe sin inventar cambios. Tras una caída, el
contacto queda invalidado hasta recibir de nuevo datos del host.

Estado local 0700 en `~/.local/state/pao-vigia` (`PAO_VIGIA_STATE_DIR` lo cambia),
socket 0600, candado por host y bitácora de conexiones/caídas. El corredor sale a
los 600 s sin esperas. Se puede terminar con SIGTERM; cierra su SSH y el flujo
sale al recibir EOF. El socket residual se reemplaza bajo el candado.
`./instalar.sh --retirar` renombra los dos archivos a `.retirado-<fecha>`; no
borra estado ni bitácoras. Terminar los corredores antes de retirar.

Pruebas (en desk): `python3 -m unittest discover -s fleet/vigia/tests -v`.
Inyección para pruebas: `PAO_VIGIA_SSH` (ejecutable/comando SSH),
`PAO_VIGIA_STATE_FILE` (archivo observado), `PAO_VIGIA_TICK`,
`PAO_VIGIA_IDLE` y `PAO_VIGIA_CONTACT` (segundos). No cambian las rutas de Pueue
ni se transmiten al host real.
