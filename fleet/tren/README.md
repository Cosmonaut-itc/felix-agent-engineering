# Tren — U1/U2/U4/U12/U13, sin repos activos

Python stdlib 3.9+, SQLite y flock POSIX. El agente registra su watch antes
de entregar; con el recibo durable termina su turno:

```sh
fleet/tren/pao-tren entregar --repo owner/project --pr 123 --head <SHA40> \
  --apto /ruta/apto.json --dirigida /ruta/dirigida.json
fleet/tren/pao-tren estado --repo owner/project
fleet/tren/pao-tren tick --repo owner/project --seco --config /ruta/config.json
```

Respuestas operativas: JSON de una línea. Exit 0: registro, lectura o plan;
exit 2: entrada/error, o tick sin `--seco` en un repo que no está inscrito
como activo (no abre el ledger; ver U13). `estado --juicio` incluye la pausa y sus causas;
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

U4 añade el adaptador y el diagnóstico al ejecutor; U13 lo conecta al tick activo.

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
del merge externo para reconstruir el contrafactual. No cambia el tick activo
ni activa escrituras; tick sin `--seco` sale con 2 en un repo en sombra.
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
del ledger siguen vigentes. Sólo el tick activo de U13 importa este ejecutor.

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
No se publican statuses ni comentarios.

Las fixtures de U4 son sintéticas, sin invocar Hasito ni Pueue. El mismo
guion remoto anterior descubre las pruebas U1/U2/U4; usar el nombre
`dirigida-tren-u4` y el SHA completo, commiteado y empujado.

## Tick activo y outbox (U13, ningún repo activo)

```sh
fleet/tren/pao-tren tick --repo owner/project --config /ruta/config.json
```

Sin `--seco`, el tick corre sólo si `inscritos` tiene la línea exacta
`owner/project`. Sale con 2 sin abrir el ledger si el repo no está inscrito
(`no-inscrito`), si está en `sombra` o si su registro es ambiguo (`ambiguo`).
Ambiguo es cualquier otra línea del repo, un modo desconocido o separadores
verticales. `merge-guard` resuelve esa duda como activo para negar merges; el
tick la resuelve como cerrada para no escribir en main. La puerta se relee
bajo el candado. El config activo añade `remoto` (URL de git, sin
credenciales) y `lanzador` (`scripts/mini/run.sh` de un checkout del repo).
`tick --seco` y la sombra no cambian. Los eventos `sombra`,
`sombra-doble-camino` y `admision-entrega` y la tabla entries siguen igual.

Un tick, sin soltar el flock del repo:

1. Lee el estado operativo, observa con `GitHubGit(activo=True)` y aplica
   `paso`. En una transacción guarda el estado y encola los efectos nuevos en
   effects/outbox. Una clave ya guardada, en cualquier estado, no se encola
   otra vez. La misma clave con otra carga es un error y revierte el paso.
2. Recorre una vez y en orden los efectos ejecutables no confirmados
   (`armar-candidato`, `solicitar-ci`, `iniciar-suite`, `reanudar-suite`,
   `repetir`, `armar-subconjunto`, `promover`). Antes de cada uno confirma
   `en-curso`. Con el carril pausado se detiene y no ejecuta nada.
3. Ejecuta con `efectos.ejecutar` y aplica `paso(estado, observación del
   efecto)`. El estado y los efectos que ese paso produce se guardan en la
   misma transacción que confirma el efecto (`hecho` y un evento
   `efecto-resultado`). Los efectos producidos así esperan al tick siguiente.

El resultado vuelve al reductor como observación ligada al tren y al intento,
el contrato que `paso` ya valida, en la transacción que confirma el efecto.
Así ningún efecto queda confirmado sin su resultado y ningún resultado se
aplica dos veces. El observador no necesita volver a descubrir resultados que
GitHub no conoce, como el recibo de una suite.
`completo=False` (Incierto o admisión perdida) registra `efecto-diferido`
y deja el efecto `pendiente`. `EfectoFallido`, `ValueError` o un error de IO
registran `efecto-fallido` con el error tipado y dejan el estado intacto. El
efecto se reintenta en cada tick y no bloquea a los demás.

El estado operativo es la instantánea del último evento `operativo`, que sólo
se escribe cuando el estado cambia o se encola un efecto, más las entregas de
entries que aún no figuran en él. La tabla trains es una proyección
consultable de esa instantánea. Nada se borra.

El observador activo lee además el CI de `C` mientras el tren está en
`EsperandoCI`; los workflows del repo deben correr en `tren/**`, o hay que
declarar `checks_esperados: []`. Un CI rojo sobre C se queda en `EsperandoCI`
sin pausar, porque el reductor no tiene esa transición. Tampoco se releen los
PR de un tren terminal.

Muerte a mitad: un efecto sin confirmar se repite sobre la instantánea
guardada y converge. El candidato es reproducible, la rama `tren/<id>` se
publica con lease vacío, el adaptador reengancha su recibo por clave y
promover es un CAS con lease. Un `promover` que quedó `en-curso` se
reconcilia antes de volver a empujar. Con `main == C` aterrizó, con
`main == B` se repite el mismo intento y con otro commit el carril se pausa
`escritor-externo`. Si el tick muere dentro de una transacción, SQLite la
revierte entera.

`devolver`, `avisar-caducidad` y el feedback de D6 no tienen ejecutor:
quedan `pendiente` en el outbox y `estado` los muestra con `ejecutor: false`.
**Activar un repo requiere la unidad que los ejecuta**, además de la
enmienda de gobierno de D2. Sin ella, un ticket devuelto o caducado no se
entera.

`estado` añade `operativo` en un repo activo o con estado operativo. Muestra
la fila FIFO, el tren activo y su fase, la pausa con su causa, los últimos
trenes terminales y cada efecto no confirmado con su estado, sus intentos y
su último registro.

Las pruebas de U13 (`tests/test_activo.py`) cubren la puerta, un lote hasta
`ATERRIZADO` con `main == C` (también por el CLI, con `gh` y lanzador falsos
en PATH), una muerte en cada frontera con conteo de actualizaciones reales de
refs en el origen, dos ticks a la vez, un main movido durante promover y un
efecto sin ejecutor. Corren con el mismo guion remoto, con el nombre
`dirigida-tren-u13-<modelo>`.
