# Tren — sombra, tick activo, feedback (U14) y juicio (U15), sin repos activados por defecto

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
`estado --juicio` añade el expediente de la pausa vigente; `juicio` y
`pausa` la resuelven (sección U15).

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

El tick activo ejecuta efectos; `--seco` conserva la simulación sin IO de escritura.

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
El tick activo publica el feedback D6 descrito abajo. Ningún repo se activa automáticamente.

Las fixtures de U4 son sintéticas, sin invocar Hasito ni Pueue. El mismo
guion remoto anterior descubre las pruebas U1/U2/U4; usar el nombre
`dirigida-tren-u4` y el SHA completo, commiteado y empujado.

## Tick activo y recuperación (U13)

`pao-tren tick --repo owner/project` sólo corre si `inscripcion.activacion`
confirma una inscripción explícita e inequívoca: `owner/project` o
`owner/project activo`, sin distinguir mayúsculas y con comentarios `#`.
Los duplicados idénticos son válidos; modos desconocidos, duplicados
contradictorios o separadores ambiguos que afectan al repo cierran la puerta.
`modo()` conserva la política conservadora del guard, donde la duda niega
merges; nunca se usa como permiso de escritura del tick.
Sin inscripción, en sombra o ante ambigüedad devuelve exit 2. Se comprueba antes de
abrir el ledger y otra vez bajo el candado. `--seco` conserva sus reductores,
eventos y continuación anteriores; jamás drena el outbox operativo.

El feedback D6 (U14) y el juicio y la pausa (U15) están implementados.
Siguen siendo requisitos de activación:

- Acreditar la enmienda de gobierno D2 y la exclusividad cooperativa D8.
- Acreditar en el lanzador real deduplicación/reenganche durable por nombre
  o evidencia e intento para suite, reanudación y repetición. Hace falta una
  prueba de contrato con muerte después de aceptar trabajo y antes de
  publicar `recibo.json`: la misma identidad debe recuperar el trabajo sin
  crear otro. El lanzador falso cumple esa condición; estas pruebas no
  acreditan al lanzador real. Su implementación pertenece a otra unidad.
- Resolver en otra unidad la salida de CI rojo sobre C: hoy el reductor
  conserva `EsperandoCI` y puede ocupar el carril indefinidamente. El injerto
  no cambia esa transición.

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
   fase, antes de readmitir PR que nuestro push cerró. Un recibo de suite,
   reanudación o repetición ya existente se valida y recupera aunque hoy haya
   cambiado el head, APTO o CI; la admisión fresca sólo permite lanzar trabajo
   nuevo. Para la suite inicial se conserva el contexto de CI verde del C con
   el que se autorizó el lanzamiento, incluso si el CI actual cambió.
3. Toma como máximo un resultado durable del buzón y aplica un solo `paso`.
   Repone únicamente los campos de resultado ligados a tren/intento; hora,
   base y vistas de admisión vienen de la observación actual. La suite inicial
   conserva también el recibo de CI con el que fue lanzada sobre C.
4. En una transacción guarda el evento `operativo` con la continuación
   completa (incluidas entradas, pausa y costo sano), la proyección `trains`,
   los efectos nuevos por clave y su outbox; confirma a la vez el resultado
   consumido. `entries` conserva sólo custodia inmutable, y las muestras de
   `admision-entrega` siguen importándose al incorporar entradas nuevas.
   La sombra nunca se toma como estado operativo ni hereda su continuación.
5. Drena en orden de inserción los efectos pendientes o fallidos conocidos,
   una vez por clave en cada tick. Antes del IO marca `ejecutando`; después
   guarda su resultado en `effect_results` y lo marca `resultado`. El tick
   siguiente lo incorpora al reductor antes de marcarlo `completo`. Un fallo
   o `completo=False` queda `fallido` para reintentar; no confirma el efecto.
   Publicar CI también exige admisión actual, aunque su clave ya estuviera
   pendiente o en ejecución antes de una muerte.

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
Una lectura incierta conserva el intento. La pausa impide despachar efectos
de git/suite; permite despachar feedback.
También se relee main al consumir un resultado de promoción ya guardado:
un ACK antiguo no oculta un escritor externo entre dos ticks.
Si la lectura previa al IO es incierta, se conserva el contrato U2: se puede
intentar el CAS protegido por lease y sólo su reconciliación decide el resultado.

Los efectos sin ejecutor (`caduca` y feedback futuro) quedan pendientes y
se saltan sin bloquear los demás. `devolver` y `avisar-caducidad` tienen el
ejecutor D6 descrito abajo. `estado` muestra
las entradas, todos los trenes con su fase, pausa/causa y el `outbox` pendiente,
en ejecución, con resultado por consumir o fallido, indicando si hay ejecutor.
La lectura usa un snapshot SQLite y no espera el flock de una suite en ejecución.

Las pruebas U13 usan un origen local, GitHub/lanzador falsos, HOME y XDG
temporales, muertes reales en fronteras de COMMIT/IO y dos procesos con flock.
Se ejecutan con el mismo guion remoto del tren, sin pruebas en la máquina
coordinadora ni escrituras a repos externos durante esa verificación.


## Feedback al dueño (U14, D6)

El dueño registra su watch del PR antes de entregar. En modo activo,
`pao-tren/resultado` es una señal al dueño y se excluye del gate de CI: un
aviso de caducidad no impide reentregar el mismo head actual. Su ciclo es
`pending` al incorporarse a un lote, `failure` al devolver o caducar y
`success` al aterrizar. Así una reentrega del head vigente reemplaza el rojo
anterior y el siguiente resultado vuelve a cambiar la señal. Los demás
checks/statuses conservan sus gates. Todo observador sin doble camino excluye
ese contexto, incluido `tick --seco` en un repo activo. La sombra con doble
camino conserva su lectura histórica de statuses y sus gates. El escritor
`feedback.EscritorGitHub`, separado del observador de sólo lectura, usa
`gh api --method POST` para comentarios y statuses; la sesión de `gh`
aporta las credenciales. No se publica configuración privada. Sólo el tick
activo drena feedback: `entregar`, la sombra y `tick --seco` no hacen POST.

Cada `devolver` publica primero un comentario en el PR culpable con lote,
B, C, head entregado, unidad, check, firma como digest `sha256:<16 hex>`,
conjunto causal y comando dirigido. El digest se calcula sobre el texto original
UTF-8; el mensaje completo del fallo sólo permanece en el ledger privado.
Unidad y check siguen siendo los identificadores públicos del test (el productor
debe mantener secretos fuera de esos identificadores). El resto de la proyección
contiene IDs de lote/entrega, SHAs y números de PR, motivos de un enum y texto
fijo; no se transportan otros campos del diagnóstico ni del config privado.
Las rutas privadas del lanzador, resultado y worktree se reemplazan por la
plantilla pública `bash scripts/mini/run.sh --repetir <resultado.json>
--sha <SHA-causal> --worktree <worktree-en-SHA> --veces 2 --unidad check=unidad`.
El dueño sustituye esas rutas por las de su entorno. Se conserva el SHA
observado de la reproducción; argumentos extra de entorno, nombre o evidencia
no se publican. El payload durable original permanece intacto en el ledger.
Después publica un status `failure`, contexto **`pao-tren/resultado`**, sobre
el **head entregado culpable**, con `target_url` al comentario y descripción
corta que remite a él. Los sobrevivientes no reciben una devolución.

`paso` emite `avisar-en-tren` por cada entrega que incorpora a un tren y
`avisar-aterrizaje` por cada entrega cuyo aterrizaje confirma. Cada efecto
lleva `(lote, recibo)` y la clave `clave(tipo, lote, recibo)`. Sólo publican
status sobre el head entregado: `pending` («En el tren; lote …») y `success`
(«Aterrizado; lote …»), sin comentario ni `target_url`. Los sobrevivientes
que forman otra generación reciben otro `pending` con su lote y clave nuevos.
La emisión es pura; el paso y su outbox comparten COMMIT, por lo que una muerte
después de guardarlos deja el status pendiente para el tick siguiente.
La simulación con `tick --seco` filtra estas dos señales antes de escribir
su evento `sombra`; conserva sus efectos históricos. `paso_sombra` y los
eventos `sombra-doble-camino` y `admision-entrega` mantienen su contrato.

Cada `avisar-caducidad` resuelve su recibo contra la entrada durable y publica
un comentario con el motivo exacto de `Rechazado` y la instrucción de volver
a entregar. Después relee el PR en GitHub y, si sigue abierto y no mergeado,
publica `failure` con el mismo contexto sobre **su head actual**. Un status
sobre el head anterior no despierta al watch que mira el actual. Cada
reintento relee ese head; no usa la vista de admisión antigua. Si el PR está
cerrado o mergeado, se conserva el comentario y no se publica status.
La lectura se hace inmediatamente antes del POST del status; un push concurrente
posterior requiere otra entrega, pues GitHub no ofrece un CAS del head para
crear statuses. No se modifican las reglas del reductor: APTO retirado,
dirigida roja o CI pendiente son retenciones, no `Rechazado`, y hoy no emiten
`avisar-caducidad`. La emisión existente cubre head cambiado y rechazos como
PR cerrado/base distinta dentro de un tren.

El comentario contiene el marcador oculto `<!-- pao-tren/efecto:<clave> -->`.
Antes de cada POST se buscan todas las páginas de comentarios del PR con
`--paginate --slurp`; si existe el marcador, se reutiliza su URL. El flock
por repo serializa el escritor. Una muerte después de aceptar el comentario
y antes de registrarlo se recupera sin otro comentario. El status se publica
siempre después: una muerte puede repetir su POST, que conserva el resultado
lógico por contexto/SHA, descripción y URL (GitHub conserva su historial).
No se actualiza un comentario ya publicado.

El drenaje de feedback ignora pausa y fase del tren, admite efectos sin tren
y no toca main ni lanza suites. Un POST o lectura fallidos deja la fila
`fallido`, visible en `estado` con error de clase pública, sin argv ni stderr.
Se reintenta en el siguiente tick. El feedback de cada PR se ejecuta en orden
estricto de emisión (inserción durable del efecto), incluso durante recuperación:
un efecto espera hasta que todos sus predecesores del mismo PR estén `completo`.
Se ordena por PR y no por SHA, pues la caducidad resuelve el head al ejecutarse.
No se descarta ninguna señal: un `pending` retrasado se publica antes del
`failure` o `success` posterior. Si un efecto anterior falla siempre, todo el
feedback posterior de ese PR queda bloqueado y visible; requiere resolver el
fallo para avanzar. Esperar no inicia un intento ni registra un fallo: la fila
conserva su estado y `estado` muestra `espera` con el motivo
`feedback-anterior-incompleto`, el PR y la clave del predecesor que la bloquea.
El feedback de otros PR y el resto del drenaje continúan sin esperar.
La observación global del tick conserva el contrato U13: si falla, el tick
no despacha IO.

Un feedback exitoso guarda en `effect_results` sólo `comentario_url` y
`status_sha` (null cuando se omite status; `comentario_url` es null para los
efectos que sólo publican status) y marca su outbox directamente
`completo` en la misma transacción. No pasa por `resultado` ni contiene una
`observacion`: el reductor y el consumo de resultados U13 no lo consumen.
Los eventos `efecto-iniciado` y `efecto-resultado` conservan su evidencia.
Git/suite mantienen su buzón y ACK transaccional de U13.

El PR del tren, el aviso de salida 75 en ese PR y la notificación de macOS
siguen pendientes como mejora (sección U15); U14 no activa repos. Continúan
pendientes los requisitos de activación listados arriba.

Las pruebas de U14 usan el mismo origen git local de U13, un escritor `gh`
falso que registra cada POST, HOME/XDG temporales, paginación y muertes
reales entre comentario/status/COMMIT. El guion `tests/remoto.sh` reproduce
las seis regresiones de R1 contra `49fd41dc`, las dos de R2 contra `0a1aabd8`
y descubre la suite completa del tren. R2 comprueba la secuencia aceptada
`failure → pending → failure` con pending fallido hasta que el lote vuelve rojo,
el avance de otro PR, la espera visible sin intentos y el observador del CLI
con `--seco`, sin POST. También cubre orden por PR entre heads distintos y
durante recuperación.
Se ejecuta sólo en el host remoto de pruebas,
con el head commiteado y empujado a la rama de trabajo.


## Juicio y pausa (U15)

Cuando el tick no puede decidir, pausa el carril. Quien juzga lee el
expediente y registra una decisión; el tick siguiente la aplica:

```sh
fleet/tren/pao-tren estado --repo owner/project --juicio
fleet/tren/pao-tren juicio --repo owner/project --tren <id> \
  --decision absolver|expulsar|reanudar|abortar [--pr N] --motivo "..."
fleet/tren/pao-tren pausa --repo owner/project --motivo "..."
fleet/tren/pao-tren pausa --repo owner/project --levantar --motivo "..."
```

`juicio` y `pausa` sólo corren con `activacion()` en `activo`; en sombra, sin
inscripción o ante ambigüedad salen con 2 sin abrir el ledger, y lo releen
bajo el candado. Bajo el candado del repo escriben la decisión y salen: no
ejecutan efectos, no tocan git ni GitHub ni el estado guardado. `--motivo`
es obligatorio y no vacío. `--tren` es el id del tren pausado y se exige
sólo para `absolver`, `expulsar` y `abortar`; `--pr` sólo para `expulsar`.
Responden `{"decision": <id>, "estado": "pendiente"}` con exit 0, o exit 2
con `error`: `sin-pausa`, `carril-pausado`, `decision-no-admitida`,
`tren-no-corresponde` (otro tren, terminal o ausente), `pr-no-expulsable`,
`falta-pr`, `pr-no-corresponde`, `sin-candidato`, `miembro-rechazado` o
`decision-pendiente`. Hay a lo sumo una decisión pendiente por repo.

**Decisión como observación.** La tabla `decisions` (id, repo, payload,
estado, resultado) es una extensión aditiva del esquema 1, como
`effect_results`; registrar escribe además un evento `decision`. El payload
guarda repo, tren, decisión, PR, motivo, instante y la `Pausa` que resuelve.
El tick toma la pendiente más antigua como `Observacion.decision` y ese paso
sólo la aplica: no consume un resultado del buzón en el mismo paso. `paso`
sigue puro: `juicio.aplicar` consulta `juicio.rechazo`, la misma función que
usa el CLI. Si la decisión ya no corresponde (la pausa cambió, el tren no es
el activo, el PR no es miembro), el estado no cambia y la fila queda
`rechazada` con ese motivo; si corresponde, `aplicada`. El cambio de estado
de la fila comparte COMMIT con el evento `operativo` del paso, que añade
`decision: {id, rechazo}`. Una muerte antes del COMMIT deja la decisión
pendiente y el estado intacto; después, aplicada una sola vez. La sombra,
`tick --seco` y sus eventos (`sombra`, `sombra-doble-camino`,
`admision-entrega`) no leen ni producen decisiones.

**Tabla motivo × decisión** (`juicio.TABLA`, una fila por motivo; para
`necesita-juicio`, por familia de detalle, el prefijo antes de `:`):

| Motivo | Detalle | Fase del tren | Admite |
|---|---|---|---|
| `etapa-1` | — | sin tren | `reanudar` |
| `escritor-externo` | — (`main` ajeno en la pausa) | tren `REARMAR`, miembros en la fila | `reanudar` |
| `manual` | motivo del operador | cualquiera | `pausa --levantar` |
| `certificado-sin-base` | — | `Certificado` sin B (U1) | `abortar` |
| `infraestructura` | `tres-reanudaciones-agotadas` | `Corriendo` o `Diagnosticando` | `absolver`, `abortar` |
| `rama-tren-ocupada` | — | `Armado` o `EsperandoCI` | `abortar` |
| `conflicto-en-cadena` | — (PR y archivos en la pausa) | `Congelado` | `expulsar` (sólo el PR en conflicto), `abortar` |
| `candidato-no-corresponde` | — | `Congelado` | `abortar` |
| `head-cambiado-en-tren` | — | cualquiera no terminal | `expulsar` (cualquier miembro), `abortar` |
| `promocion-rechazada` | — | `Promoviendo`, tres intentos | `abortar` |
| `necesita-juicio` | `incompleto-invalido`, `resultado-desconocido`, `verde-incoherente` | `Corriendo` (o `Diagnosticando` para el primero) | `absolver`, `expulsar` (cualquier miembro), `abortar` |
| `necesita-juicio` | `subconjunto-no-corresponde`, `conflicto-en-subconjunto`, `repeticion-no-corresponde`, `origen-distinto`, `presupuesto-agotado-en-reanudacion`, `suite-incompleta` | `Diagnosticando` | `absolver`, `expulsar` (cualquier miembro), `abortar` |
| `necesita-juicio` | `tamano-de-lote-no-soportado`, `sin-costo-de-suite-sana`, `sin-duracion`, `presupuesto-agotado`, `confirmacion-candidato`, `definicion-distinta`, `confirmacion-base`, `reproduccion-incierta` (de `atribucion.planificar`) | `Diagnosticando` | `absolver`, `expulsar` (cualquier miembro), `abortar` |
| motivo futuro sin fila | — | — | `abortar` |

Qué hace cada decisión:

- `reanudar` levanta la pausa y el tick sigue desde el estado guardado. Sólo
  figura donde eso avanza. En `escritor-externo` el tren ya terminó
  `REARMAR` y sus miembros volvieron a la fila con su fecha FIFO: el tick
  siguiente congela un tren nuevo sobre la `main` observada, la ajena. Nunca
  sobrescribe ese `main` ni lo da por aterrizado; la promoción sigue siendo
  el CAS con lease sobre esa base. En las demás pausas, seguir desde el
  estado guardado volvería a pausar (reanudaciones agotadas, plan de
  atribución determinista, tres intentos de promoción) o se quedaría sin
  efecto que despachar (rama ocupada, candidato o publicación ya consumidos);
  por eso no se admite.
- `absolver` descarta el resultado del tren pausado, lo cierra
  `Terminal("ABSUELTO")` y crea una generación con los mismos recibos en
  `EsperandoCI` con el mismo candidato: el mismo B y el mismo C. Esa
  generación vuelve a leer el CI de C y lanza otra suite completa con su
  propia clave; sólo una suite verde la certifica. Otro rojo vuelve a
  diagnóstico y, en su caso, a juicio. Se rechaza si algún miembro tiene la
  entrega rechazada (`miembro-rechazado`), pues el C ya no le corresponde.
  Cada miembro recibe su `pending` del lote nuevo; no se publica rojo.
- `expulsar --pr N` cierra el tren `Terminal("EXPULSADO")`, marca a N
  `Entrada.terminal=Terminal("DEVUELTO")` y emite el mismo efecto `devolver`
  de U14: comentario y status `failure` sobre el head entregado. El payload
  lleva lote, B, C (vacío si el tren aún no tenía candidato), PR, head y los
  fallos de la suite roja con N como conjunto causal y el comando de
  reproducción sobre C; fuera de `Diagnosticando` la lista de fallos va
  vacía. El campo `juicio` con el id de la decisión queda en el ledger, el
  escritor no lo publica. Los demás miembros forman, como los sobrevivientes
  de D5, una generación nueva sobre el mismo B con armado, CI y suite
  completa, y reciben su `pending`. Un miembro con la entrega ya rechazada
  (head cambiado) no sigue ni recibe otra devolución: ya tuvo su caducidad.
- `abortar` cierra el tren `Terminal("ABORTADO")` sin culpar a nadie. Sus
  entregas vuelven a la fila con `tren=None`, la misma admisión y la misma
  fecha FIFO; `entries` no cambia. No publica feedback y no escribe `main`.
  El tick siguiente las readmite por las reglas normales y forma otro tren,
  con otro id y por tanto otra rama, armado y suite.
- `pausa --motivo` registra `Pausa("manual", detalle=<motivo>)` si el
  carril no está pausado; `pausa --levantar` levanta sólo una pausa
  `manual`. Una pausa del reductor se levanta con `juicio`. Como cualquier
  pausa, detiene el consumo de resultados y el drenaje de efectos de git y
  suite; el feedback sigue publicándose. El tick que aplica la pausa ya
  recuperó antes del paso un efecto marcado `ejecutando`, como en U13.

Ninguna decisión certifica, promueve ni emite `iniciar-suite` o `promover`:
un C llega a `main` sólo por `Certificado` tras suite verde y CI verde. Un
efecto ya despachado del tren cerrado (una suite en curso) no se cancela; su
resultado queda en el buzón y el tick lo descarta por id de tren.

`estado --juicio` añade `juicio` a la salida: pausa (motivo, detalle, PR,
`main`, archivos), tren y fase, B y C, miembros con PR, head, recibo y
admisión, la causa (ruta del resultado de suite, CI y, en diagnóstico,
fallos, evidencias, ensayo pendiente, presupuesto y checks restantes), cada
decisión admitida con su comando exacto (uno por PR expulsable; sólo falta
sustituir `<motivo>`) y las decisiones registradas con su estado y motivo de
rechazo. Sin pausa, ofrece el comando de `pausa`.

Mejoras pendientes, fuera de U15: notificación de macOS al pausar,
comentario con la pausa y el expediente en un PR de tren (y el aviso de una
salida 75 allí), reintentar una promoción rechazada sin repetir la suite
(hoy sólo `abortar`) y la salida de CI rojo sobre C, que es U16.

Las pruebas U15 (`tests/test_juicio.py`) usan el origen git local de U13 y
los transportes falsos de U13/U14, con HOME y XDG temporales, invocan el CLI
(`juicio`, `pausa`, `estado --juicio`) y avanzan con `tick`. Cubren
`absolver` con otro rojo y después verde, `expulsar` en un lote de tres y
tras un head cambiado, `abortar`, `escritor-externo` con `reanudar`,
decisiones inválidas en CLI y reductor, sombra, muertes antes y después del
COMMIT que consume la decisión, la pausa manual y una comprobación de que
la tabla cubre cada `Pausa` y cada detalle literal de `dominio.py` y
`atribucion.py`. Se ejecutan con el mismo `tests/remoto.sh`, sólo en el
host remoto de pruebas.
