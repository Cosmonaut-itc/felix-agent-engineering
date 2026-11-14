# Tren — U1, desactivado

Python stdlib 3.9+, SQLite y flock POSIX. El agente registra su watch antes
de entregar; con el recibo durable termina su turno:

```sh
fleet/tren/pao-tren entregar --repo owner/project --pr 123 --head <SHA40> \
  --apto /ruta/apto.json --dirigida /ruta/dirigida.json
fleet/tren/pao-tren estado --repo owner/project
fleet/tren/pao-tren tick --repo owner/project --seco --config /ruta/config.json
```

Respuestas operativas: JSON de una línea. Exit 0: registro, lectura o plan;
exit 2: entrada/error o `activación en una unidad posterior` para tick sin
`--seco` (no abre el ledger). `estado --juicio` incluye la pausa y sus causas;
las decisiones de juicio llegan en otra unidad.

Estado: `${XDG_STATE_HOME:-~/.local/state}/pao-tren/ledger.sqlite3`, esquema 1.
Se conserva todo: requests, entries, trains, effects, events y outbox. Cada
entrega `(repo, PR, head)` tiene un recibo determinista. Repetirla conserva el
recibo y no escribe otro evento; cambiar sus referencias produce conflicto.
Un head nuevo exige otra entrega. Las rutas relativas se fijan al entregar.

`tick --seco` toma un flock por repo, observa y guarda un evento `sombra` con
estado propuesto y efectos con clave idempotente. El siguiente tick continúa
esa simulación, incorporando nuevas entregas. No altera entries operativas,
no crea filas en trains/effects/outbox ni ejecuta efectos. `estado` separa
custodia de sombra. Un fallo de observación global no guarda un plan parcial.
No hay writes de GitHub, fetch, push, suite, Pueue ni activación automática.

Configurar fuera del repo una copia de [config.example.json](config.example.json)
(por defecto `pao-tren/config.json` en el directorio de estado). Las tres
categorías sensibles son obligatorias y usan glob de `fnmatchcase` sobre la
ruta completa. Revisar los patrones de gobierno propios del repo antes de
usarlo. `git` debe apuntar a un checkout con los objetos de base/head ya
disponibles. Sin ellos, retiene; no hace fetch. La lectura `merge-tree` legacy
no escribe objetos; GitHub también debe acreditar `mergeable=true`.

Contrato de evidencia U1 (JSON UTF-8; sin Markdown alrededor):

```json
{"veredicto":"APTO","head":"<SHA40>","hallazgos_abiertos":0,"verificado":"Verificado"}
```

`verificado` se exige sólo con una etiqueta configurada de alto riesgo.
La dirigida es `{"head":"<SHA40>","resultado":"limpio"}`. APTO acepta un
archivo o URL `https://github.com/owner/project/pull/123#issuecomment-456`
cuyo cuerpo sea ese JSON. La dirigida también acepta ese formato de URL.
Las evidencias son declaraciones del review/dirigida del llamador, no una
autenticación de su autor. Un formato desconocido retiene. `heads_autorizados`
es la lista local explícita de heads sensibles autorizados por Felix; no se
deduce permiso de un comentario cualquiera. No subir configuración privada.
`gh api --method GET` usa su propia sesión; ningún token va en argv.

CI exige checks completados con success y el último status de cada contexto
verde; ausencia, skipped o neutral retienen. Todas las páginas se leen. Un
inventario de archivos truncado retiene. El head se relee antes de terminar.
Alto riesgo queda excluido por defecto en el piloto; deshabilitar
`excluir_alto_riesgo` permite planearlo solo y con Verificado. Sensibles
permanecen en merge-ready hasta autorización del head y después viajan solos.
La cola es FIFO por admisión (empates por entrega), hasta tres o 60 minutos;
un sensible/alto riesgo al frente sale solo inmediatamente. No se adelanta
un PR solo sobre tickets normales anteriores. Jale nace en pausa `etapa-1`.

Las transiciones posteriores a Corriendo son sólo tipos. U2 proveerá el
candidato y CI, U4 el adaptador y resultado; el CLI U1 no los ejecuta.

Pruebas sólo en desk, sin red y con HOME/XDG temporales:

```sh
REMOTE_RUN_HOST=desk remote-run start --carril rapido --sha <HEAD40> \
  --name dirigida-tren-u1 --script fleet/tren/tests/remoto.sh
REMOTE_RUN_HOST=desk remote-run wait <id>
```

El guion prueba cada intérprete disponible de los tres solicitados. Las
fixtures se conservan deliberadamente: el código nuevo no borra archivos.
