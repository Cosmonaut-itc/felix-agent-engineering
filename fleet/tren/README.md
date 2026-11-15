# Tren — U1/U2/U4, desactivado

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

El CLI no ejecuta efectos; U4 añade el adaptador y el diagnóstico al ejecutor explícito.

## Aterrizaje (U2, sólo ejecutor explícito)

`git_ops.GitTren(remoto, ruta=None)` usa un bare propio bajo `pao-tren/git/`
o en la ruta indicada. `armar(B, [(pr, head), ...])` trae las refs de PR y
comprueba sus heads; usa `merge-tree --write-tree` y `commit-tree` sin índice
ni checkout. Requiere git >=2.38, con error tipado si falta. Un conflicto
devuelve `ConflictoCadena(pr, archivos)` y pausa `conflicto-en-cadena`.
El SHA es reproducible con las mismas entradas y versión de git: identidad
y mensaje fijos, sin firma ni configuración global/sistema al construir.
La fecha de cada merge es el máximo timestamp de committer de B y todos los
heads, más su posición (1..N), en UTC. Sale de los objetos, nunca del reloj.
Fetch/push conservan la configuración del usuario para credenciales.
El bare sólo admite las claves y valores de configuración local que escribe
`git init --bare` (incluidos sus probes de filesystem); una clave ajena,
duplicada o `info/attributes` produce error tipado antes del fetch. Los
includes locales se rechazan sin seguirlos. Cada C queda anclado en
`refs/tren/candidatos/<C>` y el fetch desactiva el mantenimiento automático.

`efectos.ejecutar` implementa `armar-candidato`, `solicitar-ci` y `promover`
para una instantánea guardada. Publicar crea `tren/<id>` con lease vacío;
repetir con el mismo C converge, otro C produce `RamaOcupada`, ligada al tren,
ref y C esperado; el reductor pausa `rama-tren-ocupada`. Esto solicita CI sin
declararlo verde. No abre PR ni corre suite. Devuelve
`Ejecucion(observacion, completo)`: sólo `completo=True` permite confirmar el
outbox al guardar la observación. Una promoción `Incierto` queda pendiente
para repetir el mismo intento. Los demás errores de armado o publicación
quedan tipados en `EfectoFallido.resultado`, sin confirmar outbox.
Serializar el ejecutor con el candado del carril; aún no hay drenaje real.
El reductor incorpora el candidato o la pausa por conflicto/rama ocupada aun
con admisión retenida; ésta bloquea solicitar CI o promover, sin perder el
resultado completado al confirmar el outbox.

`Certificado` añade B con default vacío para leer eventos U1; uno antiguo sin
base pausa `certificado-sin-base`. Antes de pasar a `Promoviendo` se revalida
la admisión de cada miembro; un head cambiado caduca y pausa como en U1, y
una evidencia que ya no admite retiene el certificado sin emitir promoción.
Promover verifica árbol y ancestros antes
de un único push a main con `--force-with-lease=refs/heads/main:<B40>`, sin
hooks, tags ni submódulos. Siempre reconcilia leyendo el remoto después del
push, también tras un fallo de transporte. `main == C` exige árbol igual al
certificado y cada head ancestro de C. `Observacion.main` lleva ese resultado
ligado a B, C, tren e intento; `Observacion.base` sigue siendo la base para
congelar lotes, no evidencia de promoción.

Con main en B hay hasta tres intentos totales, claves estables por intento;
antes de cada reintento se readmiten los miembros. Un rechazo caduca y pausa;
una retención conserva `reintento_pendiente` sin gastar otro intento. Al
recuperarse la admisión, una lectura fresca basta para emitir el reintento.
Después de tres intentos pausa `promocion-rechazada`. El ejecutor aplica la
misma admisión antes de empujar: si no admite, sólo reconcilia, de modo que
un ACK perdido todavía puede terminar `ATERRIZADO` con los PR ya cerrados.
Una lectura incierta conserva el estado.
Otro SHA termina `REARMAR`, devuelve los miembros a la fila sin cambiar sus
fechas FIFO y pausa `escritor-externo` con el SHA observado. Al reanudar, el
reductor congela sobre la base nueva. La reconciliación de Promoviendo corre
antes de readmitir PR, pues GitHub puede haberlos cerrado tras aterrizar.
Si vuelve a una B ya usada con los mismos recibos, el id incorpora la primera
generación libre (1, 2, …); cada generación tiene sus propias claves estables
y conserva el orden FIFO de sus miembros.
Los campos añadidos tienen defaults; el encoding etiquetado y esquema 1
del ledger siguen vigentes. Ningún camino del CLI importa este ejecutor.

Pruebas sólo en desk, sin red y con HOME/XDG temporales:

```sh
REMOTE_RUN_HOST=desk remote-run start --carril rapido --sha <HEAD40> \
  --name dirigida-tren-u2 --script fleet/tren/tests/remoto.sh
REMOTE_RUN_HOST=desk remote-run wait <id>
```

El guion prueba cada intérprete disponible de los tres solicitados. Las
fixtures se conservan deliberadamente: el código nuevo no borra archivos.

## Suite y atribución (U4)

`Hasito(lanzador, git, runner=...)` ejecuta argv sin shell: `--suite C --perfil
final --base B`, y `--repetir resultado.json --sha SHA --worktree ARBOL
--veces 2 --unidad CHECK=UNIDAD`. El lanzador se configura fuera del repo;
las pruebas usan siempre un runner falso. Cada clave conserva un recibo
local; un intento interrumpido se reengancha al coordinador existente.
`GitTren.worktree(SHA)` crea desde su bare un checkout desacoplado en
`pao-tren/worktrees/<hash-del-bare>/<SHA40>`, valida HEAD, almacén y limpieza
antes de reutilizarlo y nunca lo retira. La evidencia vive en `pao-tren/hasito`.

Los lectores aceptan `suite-resultado@1` y `suite-repeticion@1`. Cruzan SHA,
base, origen, código de salida, selección, cobertura e intentos; los errores
son `ErrorResultado` dentro de `EfectoFallido` y dejan pendiente el outbox.
Los recibos anteriores sin `firma` usan `detalle`. `Completo` verde guarda
B, C, árbol, suite y recibo de CI en `Certificado`; rojo entra al diagnóstico.
Un verde contra base también certifica. Un 75 conserva `Incompleto`, nunca
se usa como fallo: espera 5, 15 y 60 minutos según `Observacion.ahora`, y
pausa `infraestructura` al agotarse las tres reanudaciones. La misma regla
se aplica a las repeticiones; su costo también consume el presupuesto.

`atribucion.planificar` es puro y emite un ensayo por vez. Confirma cada
unidad con dos intentos sobre C y dos sobre B. Exige la misma firma estable
en C y pases positivos en B para atribuir; la misma firma estable en B es
preexistente. Una firma vacía del origen requiere firmas nuevas estables y
no vacías. Ausencia, omisión, mezcla o definición distinta pasan a juicio.
Luego prueba todos los tickets solos y, si ninguno reproduce, los pares
(N<=3); C ya confirmado acredita el conjunto completo. Los archivos sólo
pueden ordenar los ensayos. Se guardan todos los mínimos observados.

Presupuesto: `min(3600 s-máquina, 0.5 * costo_suite_sana_s)`. La medida sana
viene de `Observacion.costo_suite_sana_s` o de la última suite verde limpia
guardada en `Estado`; si falta, pausa para juicio. No se estima una suite
sana desde una suite roja cortada. Cada ensayo reserva dos veces el máximo
entre segundos del check original y duraciones por intento ya observadas;
el consumo es el máximo entre reserva y costo observado. Es conservador al
no existir tiempos por unidad en el recibo original. Sin duración o sin
saldo, no despacha la reproducción. Cada reanudación reserva otro ensayo.

Los culpables reciben `Entrada.terminal=Terminal("DEVUELTO")`; `devolver`
es sólo un dato idempotente por PR, con lote, B, C, fallos, firmas, conjuntos
causales y el argv dirigido observado, con la ruta concreta del worktree.
Los sobrevivientes forman inmediatamente una generación fija sobre B,
sin incorporar nuevas entregas, y recorren armado, CI y suite completa.
No se publican statuses ni comentarios. Tick continúa desactivado.

Las fixtures de U4 son sintéticas, sin invocar Hasito ni Pueue. El mismo
guion remoto anterior descubre las pruebas U1/U2/U4; usar el nombre
`dirigida-tren-u4` y el SHA completo, commiteado y empujado.
