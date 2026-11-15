# Tren — sombra y tick activo (U13), sin repos activados por defecto

Python stdlib 3.9+, SQLite y flock POSIX. El agente registra su watch antes
de entregar; con el recibo durable termina su turno:

```sh
fleet/tren/pao-tren entregar --repo owner/project --pr 123 --head <SHA40> \
  --apto /ruta/apto.json --dirigida /ruta/dirigida.json
fleet/tren/pao-tren estado --repo owner/project
fleet/tren/pao-tren tick --repo owner/project --seco --config /ruta/config.json
```

Respuestas operativas: JSON de una línea. Exit 0: registro, lectura o paso;
exit 2: entrada/error o tick activo sin inscripción activa (no abre el ledger).
`estado --juicio` incluye la pausa y sus causas;
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
Alto riesgo exige Verificado y la autorización de su head en
`heads_autorizados`; sin ella queda retenido (`alto-riesgo-excluido-del-piloto`
mientras `excluir_alto_riesgo` sea verdadero, `merge-ready` si no). Sensibles
permanecen en merge-ready hasta autorización del head. Autorizados, ambos
viajan solos.
La cola es FIFO por admisión (empates por entrega), hasta tres o 60 minutos;
un sensible/alto riesgo al frente sale solo inmediatamente. No se adelanta
un PR solo sobre tickets normales anteriores. Jale nace en pausa `etapa-1`.

El CLI no ejecuta efectos; U4 añade el adaptador y el diagnóstico al ejecutor explícito.

## Sombra con doble camino (U12)

El registro `${XDG_CONFIG_HOME:-~/.config}/pao-tren/inscritos` admite
`owner/project` (activo) y `owner/project sombra`, sin distinguir mayúsculas
y con comentarios `#`. Modo desconocido o duplicado activo prevalece sobre
sombra. Registro ilegible conserva la negación del guard. En sombra el ticket
entrega con APTO, recibo y watch, consulta `tick --seco` y `estado` y después
completa los gates y merge habituales. Una retención/rechazo se anota y no
frena ese merge; el ticket no edita `heads_autorizados`.
Ambos lectores separan registros sólo por CR/LF. Si una línea contiene
separadores verticales o Unicode (`VT`, `FF`, U+001C–U+001E, U+0085,
U+2028/U+2029), todos sus tokens `owner/repo` cuentan como activos, incluso
tras `#`: una línea ambigua nunca concede sombra.

`entregar` observa la admisión D7 en ese momento y la guarda atómicamente
con la custodia en un evento `admision-entrega`. Acepta `--config` como tick;
sin lectura/config válida conserva custodia y registra retención por
observación incompleta. Reentregar el mismo head no cambia esa muestra.
Sólo para un repo inscrito en sombra, `tick --seco` usa un reductor separado:
los lotes pasan directamente a `SIMULADO`, sin efectos ni espera de suite.
Su continuación se guarda en eventos `sombra-doble-camino`; los eventos
`sombra` de U1/U2/U4 siguen perteneciendo al reductor activo. Al cambiar la
inscripción a activo, los PR abiertos se readmiten desde custodia y sus
muestras iniciales, sin arrastrar lotes simulados.
Un PR que GitHub confirma mergeado en main con el head entregado termina
`MERGEADO-FUERA`; cerrado sin merge sigue siendo rechazo. Los avances de
main no pausan la sombra. La primera admisión se conserva incluso después
del merge externo para reconstruir el contrafactual. No activa escrituras;
el tick sin `--seco` exige inscripción activa explícita.
Un rechazo conserva esa admisión histórica y registra `caducada`, el instante
en que se observó. La entrega sale de la fila desde entonces; un lote cerrado
antes queda intacto. Si coincide con el cierre, la caducidad gana. El merge
externo confirmado conserva su muestra. El campo tiene default para leer
eventos anteriores; en medición, su ausencia usa el primer rechazo observado.

```sh
fleet/tren/pao-tren medir --repo owner/project \
  --desde 2026-11-01T00:00:00Z --hasta 2026-11-08T00:00:00Z
```

`medir` abre SQLite en modo de sólo lectura, sin crear directorios ni ledger.
Imprime una línea JSON determinista: entregas en `[desde, hasta)`, admisión
al entregar y motivo, primera admisión observada, lotes, distribución 1/2/3,
pendientes y `suites_ahorradas_estimadas = muestras en lotes o pendientes - lotes`.
`admitidas` conserva la cuenta histórica; las entregas caducadas antes de un
lote no producen ahorro. Simulación y medición usan la misma cola temporal.
Los eventos U1
se leen igual; sin muestra al entregar se informa custodia y fuente
`sin-observacion-al-entregar`, sin inventar evidencia retroactiva.
Los lotes usan los tiempos de primera admisión, FIFO hasta 3 o 60 min;
sensibles y alto riesgo admitidos viajan solos sin adelantar al primero.
A los 60 min vence la ventana antes de incorporar una llegada simultánea.
La ventana selecciona la cohorte de entregas y sólo sus observaciones
anteriores a `hasta`; un lote aún sin vencer figura en pendientes. El ahorro
es provisional mientras haya pendientes. No estima duración ni resultado
de suites: supone disponibilidad inmediata entre lotes, incluso si el merge
externo ocurrió antes de que se cerrara la ventana virtual.

## Instalación en macOS (U7, sombra)

Desde un checkout estable de PAO, `bin/pao sync --no-plugins --no-schedule`
enlaza `~/.local/bin/pao-tren` al CLI; éste resuelve sus módulos incluso con
`python3 -I ~/.local/bin/pao-tren --help`. Preparar primero el config privado
descrito arriba, con un checkout que ya contenga los objetos necesarios.
El instalador necesita `python3`, `gh` y `git` en PATH:

```sh
python3 -I fleet/tren/instalar.py --repo owner/project
# Revisar el plist y ejecutar el launchctl bootstrap impreso, o usar:
python3 -I fleet/tren/instalar.py --repo owner/project --cargar
# Retirar sin borrar el plist, los logs ni el ledger:
python3 -I fleet/tren/instalar.py --repo owner/project --retirar
```

Cada repo tiene un label `local.pao.tren.<sha256-del-repo>` en
`~/Library/LaunchAgents/`. Corre `pao-tren tick --repo owner/project --seco`
al cargar y cada 120 segundos. El plist fija HOME, XDG y PATH, incluidos los
directorios de los ejecutables detectados; los logs quedan bajo
`${XDG_STATE_HOME:-~/.local/state}/pao-tren/launchd/<label>/`.
No se carga por defecto; `--cargar` recarga si ya está cargado, para aplicar
también cambios preparados antes. Repetir una instalación idéntica conserva el plist;
un cambio guarda `.bak-*` antes del reemplazo atómico. `--retirar` hace
bootout si está cargado y renombra a `.retirado-*`. Nada se borra.
La activación y los siete días de sombra requieren una decisión posterior.
CI prueba la suite en Ubuntu y macOS, también con `/usr/bin/python3` en macOS;
Windows queda fuera por flock POSIX.

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
El tick activo serializa el ejecutor con el candado del carril.
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
del ledger siguen vigentes. Sólo el tick activo importa este ejecutor.

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
Sin culpables, certifica sólo si la suite no dejó `restantes`; si el
fail-fast cortó checks, pausa `necesita-juicio` con `suite-incompleta`.

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
No se publican statuses ni comentarios. Ningún repo se activa automáticamente.

Las fixtures de U4 son sintéticas, sin invocar Hasito ni Pueue. El mismo
guion remoto anterior descubre las pruebas U1/U2/U4; usar el nombre
`dirigida-tren-u4` y el SHA completo, commiteado y empujado.

## Tick activo y recuperación (U13)

`pao-tren tick --repo owner/project` sólo corre si `inscripcion.modo` devuelve
`activo`. Sin inscripción o en sombra devuelve exit 2. Se comprueba antes de
abrir el ledger y otra vez bajo el candado. `--seco` conserva sus reductores,
eventos y continuación anteriores; jamás drena el outbox operativo.

**Activar un repo requiere primero la unidad de feedback D6**, que todavía
falta: devolver resultados al dueño, caducidades, statuses y comentarios.
Esta unidad no activa repos, no instala tareas y no cambia el gobierno.

Además de la configuración de admisión, el tick activo requiere `remoto`
explícito (URL de git sin credenciales o ruta a un origen). `git` sigue siendo
el checkout local con los objetos necesarios para observar admisión. Para
usar Hasito, `lanzador_hasito` es la ruta absoluta a su lanzador existente;
sin adaptador, los efectos de suite quedan fallidos y visibles, reintentables.
`costo_suite_sana_s` puede aportar el presupuesto de diagnóstico antes de que
el ledger tenga una suite sana. Estos datos viven en el config privado:

```json
{
  "remoto": "https://github.com/owner/project.git",
  "lanzador_hasito": "/path/to/project/scripts/mini/run.sh",
  "costo_suite_sana_s": 3600
}
```

Son campos adicionales del objeto `repos["owner/project"]`, no otro archivo.
El observador activo consulta el CI del C exacto cuando espera CI; publicar
la rama nunca acredita que esté verde. Un error global de observación no
guarda un paso parcial ni despacha trabajo. Los errores del ejecutor quedan
registrados con datos tipados o clase pública, sin argv ni stderr privados.

Todo el ciclo mantiene el flock del repo, incluido git y el adaptador:

1. Lee la continuación operativa y observa GitHub/git en modo activo.
   Al incorporar una entrega nueva conserva su muestra inicial de admisión;
   ninguna generación simulada ni ningún evento de sombra se vuelve operativo.
2. Recupera efectos marcados `ejecutando` por un proceso muerto, sobre esa
   fase y con admisión fresca, antes de readmitir PR que nuestro push cerró.
3. Toma como máximo un resultado durable del buzón y aplica un solo `paso`.
   Repone únicamente los campos de resultado ligados a tren/intento; hora,
   base y vistas de admisión vienen de la observación actual. La suite inicial
   conserva también el recibo de CI con el que fue lanzada sobre C.
4. En una transacción guarda el evento `operativo` con la continuación
   completa (incluidas pausa y costo sano), las proyecciones `entries` y
   `trains`, los efectos nuevos por clave y su outbox; confirma a la vez el
   resultado consumido. La sombra nunca se toma como estado operativo.
5. Drena en orden de inserción los efectos pendientes o fallidos conocidos,
   una vez por clave en cada tick. Antes del IO marca `ejecutando`; después
   guarda su resultado en `effect_results` y lo marca `resultado`. El tick
   siguiente lo incorpora al reductor antes de marcarlo `completo`. Un fallo
   o `completo=False` queda `fallido` para reintentar; no confirma el efecto.

`effect_results` es una extensión aditiva del esquema 1. Los eventos de
sombra y su encoding no cambian. El ACK del resultado y el paso consumidor
comparten COMMIT: una muerte deja ambos pendientes o ambos guardados.
No se pierde un candidato construido aunque la nueva admisión retenga el PR.

Ante una muerte tras el IO, armar reconstruye el mismo C; solicitar CI relee
la rama con lease; Hasito reutiliza su recibo por clave, escrito con rename
atómico y fsync. Si aún no hay recibo, vuelve a llamar al lanzador con los
mismos nombre/evidencia para reenganchar su coordinador idempotente. Esto
requiere ese contrato del lanzador: no se puede garantizar una sola suite
con un lanzador que cree otro trabajo al recibir el mismo identificador.
Promover primero reconcilia: `main == C` verifica árbol/ancestros y confirma
sin otro push; `main == B` permite el CAS; otro SHA pausa por escritor externo.
Una lectura incierta conserva el intento. La pausa impide despachar efectos.
También se relee main al consumir un resultado de promoción ya guardado:
un ACK antiguo no oculta un escritor externo entre dos ticks.
Si la lectura previa al IO es incierta, se conserva el contrato U2: se puede
intentar el CAS protegido por lease y sólo su reconciliación decide el resultado.

Los efectos sin ejecutor (`devolver`, `avisar-caducidad`, `caduca` y feedback
futuro) quedan pendientes y se saltan sin bloquear los demás. `estado` muestra
las entradas, todos los trenes con su fase, pausa/causa y el `outbox` pendiente,
en ejecución, con resultado por consumir o fallido, indicando si hay ejecutor.
La lectura usa un snapshot SQLite y no espera el flock de una suite en ejecución.

Las pruebas U13 usan un origen local, GitHub/lanzador falsos, HOME y XDG
temporales, muertes reales en fronteras de COMMIT/IO y dos procesos con flock.
Se ejecutan con el mismo guion remoto del tren, sin pruebas en la máquina
coordinadora ni escrituras a repos externos durante esa verificación.
