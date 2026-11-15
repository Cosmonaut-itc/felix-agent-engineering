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

CI exige la corrida más nueva de cada check completada con success y el
último status de cada contexto verde; una corrida cancelada que un re-run
reemplazó no cuenta. Ausencia, skipped o neutral retienen. `checks_esperados`
(opcional) lista nombres que deben existir; `[]` declara un repo sin CI, y
sólo entonces cero checks cuenta como verde. Todas las páginas se leen. Un
inventario de archivos truncado retiene. El head se relee antes de terminar.
Alto riesgo queda excluido por defecto en el piloto; deshabilitar
`excluir_alto_riesgo` permite planearlo solo y con Verificado. Sensibles
permanecen en merge-ready hasta autorización del head y después viajan solos.
La cola es FIFO por admisión (empates por entrega), hasta tres o 60 minutos;
un sensible/alto riesgo al frente sale solo inmediatamente. No se adelanta
un PR solo sobre tickets normales anteriores. Jale nace en pausa `etapa-1`.

Las transiciones de Corriendo a Certificado/Diagnosticando son sólo tipos:
U4 aporta el adaptador y el resultado de la suite.

## Aterrizaje (U2)

[git_ops.py](git_ops.py) opera sobre un clon bare propio
(`${XDG_STATE_HOME:-~/.local/state}/pao-tren/git/<repo>.git`) y un remoto
explícito. Necesita git 2.38 o posterior; si no, `GitAntiguo`.

- **Armar.** Trae `refs/pull/<n>/head` y exige que coincida con el head
  entregado. `C_0 = B`, `C_i = merge(C_{i-1}, H_i)` con
  `merge-tree --write-tree` y `commit-tree`, sin índice ni working tree.
  Autor y committer `pao-tren`, sin firma, mensaje derivado de `B`, PR y
  head. La fecha de cada eslabón es la mayor fecha de committer de sus dos
  padres, en UTC: sale sólo de objetos direccionados por contenido. La
  construcción ignora la configuración global, de sistema y la inyectada por
  entorno, así que otro clon da el mismo `C`. Un choque devuelve
  `ConflictoCadena(B, pr, archivos)` y pausa `conflicto-en-cadena`.
- **Publicar para CI.** `C` a `refs/heads/tren/<id>` con lease de creación.
  Si ya apunta a `C`, éxito; si apunta a otro commit, `RamaOcupada` y pausa
  `rama-tren-ocupada`. No se sobrescribe.
- **Promover.** Un solo push:
  `--force-with-lease=refs/heads/main:<B40> <remoto> <C40>:refs/heads/main`,
  con `--no-verify`, sin tags ni hooks locales. Fetch y push usan la
  configuración del usuario por las credenciales; ningún token va en argv.
- **Reconciliar.** Tras todo push, incluso fallido, lee `main` con
  `ls-remote`: `Aterrizado` (`main == C`, árbol certificado y cada head
  ancestro de `C`), `EnBase`, `EscritorExterno(sha)` o `Incierto`.

En el reductor, `Certificado(…, B)` pasa a `Promoviendo(intentos=1)` y emite
`promover`. En `Promoviendo` decide sólo la lectura de `main`, porque el push
pudo haber llegado ya: aterrizado termina `ATERRIZADO`; en base reintenta con
clave nueva hasta 3 intentos y después pausa `promocion-rechazada`; escritor
externo termina el tren `REARMAR`, devuelve sus miembros a la fila con su
`admitida` (orden FIFO intacto) y pausa `escritor-externo` con `main=<sha>`.
Al levantar la pausa, el lote se forma de nuevo sobre el `main` nuevo.
Incierto no cambia nada; el outbox repite el efecto (`Ejecucion.completo`).

`Ejecutor` corre `armar-candidato`, `solicitar-ci` y `promover` y devuelve la
observación del paso siguiente. Sólo lo llaman las pruebas: `tick` sin
`--seco` sigue saliendo con 2 y ningún camino del CLI escribe en un remoto.

Pruebas sólo en desk, sin red y con HOME/XDG temporales:

```sh
REMOTE_RUN_HOST=desk remote-run start --carril rapido --sha <HEAD40> \
  --name dirigida-tren-u1 --script fleet/tren/tests/remoto.sh
REMOTE_RUN_HOST=desk remote-run wait <id>
```

El guion prueba cada intérprete disponible de los tres solicitados. Las
fixtures se conservan deliberadamente: el código nuevo no borra archivos.
