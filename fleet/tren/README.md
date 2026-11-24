# Tren — sombra, tick activo, feedback, juicio y CI sobre C (U16), sin repos activados por defecto

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
`estado --juicio` incluye el expediente, los comandos admitidos y el historial
durable de decisiones con su resultado; véase U15 al final.

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
categorías de `sensibles` son obligatorias y usan glob de `fnmatchcase` sobre
la ruta completa. La clave opcional `sensibles_en_lote` lista categorías de
`sensibles` que permiten viajar en lote, por ejemplo `["gobierno"]`. Toda
categoría de la lista debe existir en `sensibles`. Un PR sensible puede viajar
en lote si todas las categorías que coinciden con sus archivos figuran en la
lista. Si la clave se omite o la lista está vacía, toda categoría sensible
fuerza un tren propio. En todos los casos se exige autorización por head.
Revisar los patrones de gobierno propios del repo antes de usarlo.
En sombra, `entregar` y `tick --seco`, `git` debe apuntar a un
checkout con los objetos de base/head ya disponibles. Sin ellos, retiene; no
hace fetch. El tick activo no lee ese checkout: usa el bare del tren y trae
lo que falte (véase U17). La lectura `merge-tree` legacy no escribe objetos;
GitHub también debe acreditar `mergeable=true`.

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
permanecen en merge-ready hasta autorización del head. El alto riesgo fuerza
un tren propio si la clave booleana opcional `alto_riesgo_en_lote` se omite o
vale `false`. Con `true`, un PR de alto riesgo admitido puede viajar en lote
si todas sus categorías sensibles figuran en `sensibles_en_lote`. En el tick
activo, una ventana de migración también fuerza un tren propio.
El primer tick con el tren libre forma un lote de hasta cinco PRs de la cola
FIFO por admisión, con desempate por hora de entrega y recibo. Mientras un
tren está activo, los admitidos esperan el siguiente lote. Si un PR requiere
tren propio, el lote se corta antes de él. Cuando llega al frente, sale solo.
Jale nace en pausa `etapa-1`.

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
los lotes pasan directamente a `SIMULADO`, sin efectos ni ejecución de suites.
Cada lote simula 68 minutos de ocupación, la mediana medida de los trenes de
Jale que recoge el encargo SEN-7119 (ronda 2).
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
al entregar y motivo, primera admisión observada, lotes, distribución de 1 a 5,
pendientes y `suites_ahorradas_estimadas`.
`admitidas` conserva la cuenta histórica. `modo` indica el último tramo
observado antes de `hasta` (`activo` o `sombra`). Cada lote contiene
`instante`, `motivo`, `recibos`, `prs`, `tamano` y `fuente` (`real` o `sombra`).
El historial conserva los lotes de ambas fuentes. Para la distribución y el
ahorro, cada recibo cuenta una vez y tiene prioridad su lote real. Cada lote
de sombra aporta los recibos que quedan, en orden de despacho. Los lotes sin
recibos únicos se omiten del cálculo. El ahorro es el número de recibos únicos
en lotes o pendientes menos el número de lotes que aportan recibos.
Los eventos U1 se leen igual. Sin muestra al entregar se informa custodia y fuente
`sin-observacion-al-entregar`, sin inventar evidencia retroactiva.
La entrega y el tick en sombra aplican `sensibles_en_lote` y
`alto_riesgo_en_lote` al formar lotes.
En sombra, `medir` proyecta con la cola FIFO y la ocupación estimada de 68
minutos. Al activar el tren, conserva los despachos simulados anteriores al
primer snapshot `operativo`. Los PR que aún esperaban pasan al tramo activo.
Cada lote real toma sus miembros y su instante del primer snapshot
`operativo` en que aparece. Al corte, el último snapshot activo aporta
los pendientes que siguen admitidos y sin tren.
Al volver a sombra, el despacho empieza en el cambio de modo y los
pendientes conservan su orden de admisión original.
La historia anterior a `desde` también cuenta para formar los lotes. El
informe muestra los miembros de la cohorte `[desde, hasta)` y sólo usa
observaciones anteriores a `hasta`.

### Duración por fase

```sh
fleet/tren/pao-tren fases --repo owner/project \
  --desde 2026-11-01T00:00:00Z --hasta 2026-11-08T00:00:00Z
```

`fases` lee SQLite sin candado ni inscripción activa e imprime una línea JSON.
Incluye cada tren cuyo cierre cae en `[desde, hasta)` y sigue su historial
hasta el último evento disponible, incluso después de `hasta`. Los hitos son
segundos desde el epoch y usan el instante de arranque del tick que registró
el evento. Los tramos tienen resolución de un tick.

- `cierre`: primer estado operativo que contiene el tren.
- `ci_solicitado`: primer resultado de `solicitar-ci`.
- `suite_iniciada`: primer inicio de `iniciar-suite`, cuando se observó CI verde.
- `suite_terminada`: último paso operativo que consume el resultado de
  `iniciar-suite` o `reanudar-suite`. El resultado del efecto lleva el
  instante de arranque del tick que lo registra: el que lanzó la suite o,
  tras una interrupción, el que recupera su recibo. Ese instante no es el
  fin de la suite; por eso el fin lo marca el paso que consume el resultado.
  El hito vale `null` hasta el primer consumo y cada consumo posterior lo
  actualiza.
- `certificado`: primer estado operativo en `Certificado` o `Promoviendo`.
- `fin`: primer estado operativo en `Terminal`, con su resultado.

Los tramos son diferencias en segundos: `armado_s` del cierre a la solicitud
de CI, `ci_s` de la solicitud al inicio de suite, `suite_s` del inicio al
fin de suite, `promocion_s` del fin de suite al fin y `total_s` del cierre
al fin. Un hito ausente o un tramo sin alguno de sus extremos vale `null`.

Un verde directo aterrizó sin diagnóstico, reanudación de suite ni pausa
mientras estaba vivo. El resumen de verdes directos usa el rango más cercano
(`ceil(p × n)`, contando desde uno) para p50 y p90, y el máximo por tramo.
Cada estadística usa los valores disponibles y vale `null` si no hay muestra.
Los demás trenes terminados se cuentan por resultado. Los vivos aparecen en
el detalle y quedan fuera del resumen.

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
# Tick activo, sin --seco, con la inscripción ya en activo:
python3 -I fleet/tren/instalar.py --repo owner/project --activo --cargar
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
`--activo` prepara el mismo label con `pao-tren tick --repo owner/project`,
sin `--seco`. El tick activo sale con 2 mientras la inscripción no esté en
activo, así que primero se cambia `inscritos` y después se carga el plist.
Volver a instalar sin `--activo` restaura el tick en sombra; el plist activo
queda en `.bak-*`.
CI prueba la suite en Ubuntu y macOS, también con `/usr/bin/python3` en macOS;
Windows queda fuera por flock POSIX.

### Ticks encadenados

El tick activo repite el tick en el mismo proceso mientras el anterior haya
hecho avanzar el carril, hasta ocho veces. Avanzar es que cambie la pausa, la
fase de algún tren o el estado de algún efecto del outbox. Un efecto que
falla cuenta como pendiente, así que la cadena no lo reintenta por sí solo.
La cadena necesita una huella antes y después del tick para comparar. El
primer tick de un repositorio crea el ledger y cede el siguiente a launchd.

Si la huella queda igual y un tren está en `EsperandoCI`, el proceso sondea
el CI de ese candidato cada 20 s, hasta 100 s desde el arranque del proceso.
El plazo deja margen antes del siguiente disparo de launchd, a los 120 s.
La espera lee el ledger en sólo lectura, sin tomar el candado, y cierra la
conexión antes de dormir. Corta si hay una pausa o una decisión pendiente,
si cambia el tren, su fase, su C o su solicitud de CI, si falla la lectura
del ledger o de la configuración, o si ya no cabe otro intervalo.
Un CI verde o rojo da paso a otro tick si la cuota y el programa todavía
lo permiten. Si ese tick deja la huella igual, la cadena termina.

El plazo acota la espera, no los ticks. El tick que sigue a la espera puede
arrancar unos segundos después del plazo, porque la consulta de cuota y la
lectura del ledger tienen su propio tiempo de espera, y la cadena continúa
después como cualquier otra. Los cortes de la espera sólo evitan esperar en
vano. Una pausa o una decisión que llegue después del último corte la aplica
ese tick, bajo el candado del carril, como en cualquier otro tick.

Cada tick de la cadena toma y suelta el candado del carril y observa de nuevo.
`stdout.log` recibe una línea JSON por tick y el proceso sale con el código
del último. `tick --seco` corre un solo tick.

La cadena termina también en dos casos:

- Cambió algún módulo instalado de `fleet/tren` desde que el proceso arrancó.
  El tick en curso acaba con el código anterior y launchd lanza el siguiente
  con el nuevo.
- A la cuota de la API de GitHub le quedan menos de 1,000 llamadas, o no se
  pudo leer.

### Instalar una versión nueva

Todos los carriles corren el mismo programa instalado. Instala con el candado
del programa tomado:

```sh
pao-tren candado -- git -C <checkout instalado> merge --ff-only <sha>
```

El candado del programa es `programa.lock`, en el directorio de estado. Cada
proceso `pao-tren tick`, activo o `--seco` y de cualquier carril, lo toma
compartido al arrancar y lo conserva hasta salir: mientras corre sus ticks,
mientras espera el CI y mientras espera una suite.

El coordinador de Jale prelanzado puede sobrevivir al tick y corre sin el
candado del programa. Python lo ejecuta en modo aislado con importaciones
de la biblioteca estándar. El tick espera hasta 5 s a que el coordinador
tome el candado de la corrida o termine, para que cargue bajo el candado
del programa. El coordinador ya podía sobrevivir sin ese candado cuando
moría el tick que esperaba su resultado.

`candado` lo toma en exclusiva sin esperar y corre el comando. Después
conserva el candado 5 s más, lo suelta y sale con el código del comando. Su
última línea es `{"candado": "tomado", "codigo": N}`. Si hay algún proceso de
tick vivo, sale con 75 y `{"candado": "ocupado"}` sin correr el comando. No
crea el ledger: sin él sale con 2.

Un proceso de tick que arranca mientras alguien instala escribe
`{"omitido": "instalacion-en-curso"}` y sale con 0. No abre el ledger, no
observa, no encadena y no espera el CI. Esa línea en `stdout.log` no es un
tick; el siguiente disparo de launchd corre con el programa nuevo. Un tick
activo sin inscripción activa, o con el registro de inscritos ilegible,
responde su error de siempre y sale con 2. Un proceso que arranca a media
instalación pudo cargar módulos de dos versiones; los 5 s de más cubren su
arranque, porque prueba el candado en cuanto termina de cargar.

Cuándo sale 75:

- Con un tren en `EsperandoCI`, el proceso del tick activo vive hasta 100 s
  de cada 120. Reintenta cada pocos segundos hasta caer en el hueco:
  `until pao-tren candado -- <comando>; [ $? -ne 75 ]; do sleep 2; done`.
- El tick que espera una suite retiene el candado todo ese tiempo, que en
  Jale ronda los 90 minutos. `candado` no espera ni interrumpe ese tick. Si
  la cola del host de pruebas está en pausa o retenida, la suite no termina
  sola y la instalación pide antes resolver esa cola.

Límites conocidos:

- Sólo queda cubierta una instalación hecha con `candado`.
- `estado`, `entregar`, `juicio`, `pausa` y los comandos de medición no toman
  el candado del programa.
- Un proceso que tarda más de 5 s en cargar a media instalación no queda
  cubierto.
- Sin directorio de estado no hay candado: el primer tick de una máquina, que
  lo crea, corre sin él.
- Si `pao-tren candado` muere por una señal, su comando hijo sigue sin el
  candado.
- Un proceso que arranca a media instalación puede fallar al importar un
  módulo de cabecera y salir con 1 antes de llegar al candado. No abre el
  ledger, y el siguiente disparo de launchd corre normal.

## Aterrizaje (U2, sólo ejecutor explícito)

`git_ops.GitTren(remoto, ruta=None)` usa un bare propio bajo `pao-tren/git/`
o en la ruta indicada. `armar(B, [(pr, head), ...])` trae las refs de PR y
comprueba sus heads; usa `merge-tree --write-tree` y `commit-tree` sin índice
ni checkout. Requiere git >=2.38, con error tipado si falta. Un conflicto
devuelve `ConflictoCadena(pr, archivos)` y pausa `conflicto-en-cadena`.
El SHA es reproducible con las mismas entradas y versión de git: identidad
y mensaje fijos, sin firma ni configuración global/sistema al construir.
La identidad de autor y committer es `PAO Tren <tren@example.invalid>`. La
clave opcional `identidad_commits` del repo, con `nombre` y `email`, la
sustituye. Sirve cuando el deploy de `main` verifica al autor del commit y
bloquea una cuenta que GitHub no conoce. Una identidad inválida falla al leer
la configuración. Los candidatos ya armados conservan la identidad con la que
se construyeron.
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
no vacías. La tabla de pausas detalla cuándo una repetición permite absolver
un intermitente.
Luego prueba todos los tickets solos y, si ninguno reproduce, las combinaciones
de 2 hasta N-1, con N hasta `LIMITE_LOTE`, el mismo tope que forma los lotes;
C ya confirmado acredita el conjunto completo. El presupuesto acota los ensayos. Los archivos sólo
pueden ordenar los ensayos de atribución. Se guardan todos los mínimos observados.
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

El feedback D6 está implementado. Siguen siendo requisitos de activación:

- Acreditar la enmienda de gobierno D2 y la exclusividad cooperativa D8.
- Acreditar en el lanzador real deduplicación/reenganche durable por nombre
  o evidencia e intento para suite, reanudación y repetición. Hace falta una
  prueba de contrato con muerte después de aceptar trabajo y antes de
  publicar `recibo.json`: la misma identidad debe recuperar el trabajo sin
  crear otro. Jale acredita este requisito con la prueba de contrato de su
  lanzador real por `REMOTE_RUN_CLAVE` y la recuperación del coordinador por
  esa misma clave (U23, véase U9). Las pruebas del lanzador falso del tren
  verifican la integración; por sí solas no acreditan otros lanzadores reales.

Un sincronizador externo del checkout `git` no es requisito de activación:
desde U17 el tick activo trae al bare del tren la base y los heads que le
falten (véase U17).

Esta unidad no activa repos, no instala tareas y no cambia el gobierno.

Además de la configuración de admisión, el tick activo requiere `remoto`
explícito (URL de git sin credenciales o ruta a un origen). La admisión del
tick activo lee el bare del tren de ese `remoto`; `git` sólo lo usan la sombra
y `tick --seco`. Para
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
En Hasito se publica la plantilla `bash scripts/mini/run.sh --repetir <resultado.json>
--sha <SHA-causal> --worktree <worktree-en-SHA> --veces 2 --unidad check=unidad`.
En Jale se publica `bash scripts/test-hosts/run-remote.sh <host> <log>
<modo> <SHA-causal> <unidad>`. El dueño sustituye las rutas de resultado,
worktree o log por las de su entorno. En ambos casos se usa el SHA de la
reproducción observada o el SHA de la evidencia si ésta carece de comando.
Una devolución por juicio usa el SHA del candidato. El payload durable
original permanece en el ledger.
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
siguen como mejoras pendientes. Continúan pendientes los requisitos de
activación listados arriba.

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

## Juicio y pausa manual (U15)

```sh
pao-tren estado --repo owner/project --juicio
pao-tren juicio --repo owner/project --tren <id> --decision absolver --motivo 'Repetir el lote'
pao-tren juicio --repo owner/project --tren <id> --decision expulsar --pr 123 --motivo 'Devolver al dueño'
pao-tren juicio --repo owner/project --tren <id> --decision abortar --motivo 'Readmitir las entregas'
pao-tren juicio --repo owner/project --tren <id> --decision reanudar --motivo 'Causa resuelta'
pao-tren pausa --repo owner/project --motivo 'Mantenimiento'
pao-tren pausa --repo owner/project --levantar --motivo 'Mantenimiento terminado'
```

`juicio` y `pausa` exigen `activacion(repo) == activo`, antes de abrir el
ledger y bajo su flock. En sombra, sin inscripción o con inscripción ambigua
salen con 2. No activan repos, ejecutan efectos, leen recibos ni llaman a
GitHub. El motivo es obligatorio y no puede estar vacío. `--tren` es
obligatorio cuando la pausa corresponde a un tren; se omite para `etapa-1`.
`--pr` es obligatorio para expulsar y se rechaza en las otras decisiones.
Una pausa automática no puede sustituirse ni levantarse con `pausa`.

Los comandos registran una entrada `pendiente` en la tabla aditiva
`decisions` y el evento `juicio-registrado`: id, repo, tren, acción, PR,
motivo, instante y copia/identidad de la pausa que resuelve. Admiten una
entrada pendiente por repo. El siguiente tick activo consume esa entrada
**antes de recuperar cualquier efecto git/suite** y dedica el paso entero a
ella. Así, una pausa manual registrada ya impide el siguiente despacho,
incluso de un efecto que quedó `ejecutando`. Hasta ese tick, `estado --juicio`
muestra la orden pendiente y el snapshot anterior. El feedback sigue
publicándose durante la pausa y respeta el orden de U14. `tick --seco` no
consume decisiones ni cambia esta continuación operativa.

Mientras el carril está en pausa, se conserva la admisión de los miembros
de un tren en `Promoviendo` cuyo último push no tiene resultado consumido:
pudo aterrizar con su ACK aún guardado o perdido tras una muerte. Sus
caducidades se difieren hasta la reconciliación de main al reanudar, antes de
readmitirlos; si aterrizó, sus entregas quedan resueltas sin avisos falsos.
Si el tick ya consumió que el push no aterrizó (`EnBase`), como en
`promocion-rechazada`, nada se difiere: una caducidad real se publica durante
la pausa y sobrevive a `abortar` o `expulsar`. Las demás entregas siguen
readmitiéndose y el feedback pendiente sigue drenándose durante la pausa.

La identidad de la pausa es la primera secuencia `operativo` de su aparición
(más el caso de la pausa inicial), no sólo su texto. Ticks pausados conservan
la identidad; levantar y volver a pausar la renueva. Los snapshots anteriores
y los contratos de `Estado`, `Pausa` y eventos de sombra no añaden campos.
`paso` recibe la decisión y la identidad vigente en su observación y vuelve a
validar repo, pausa, tren, miembro, fase y política. Un juicio ya obsoleto se
registra `rechazada` sin modificar el estado. Si el CLI puede determinar el
error, devuelve 2 sin registrarlo. Detalles desconocidos cierran la puerta.

`resolver-juicio` es un resultado interno del reductor, no un efecto de IO:
`guardar_paso` guarda en una sola transacción su resultado (`aplicada` o
`rechazada`), `juicio-resuelto`, el snapshot, las proyecciones y el outbox.
Una muerte tras registrar la decisión conserva la entrada pendiente; una
muerte tras reducir pero antes de COMMIT la vuelve a intentar; después del
COMMIT no la vuelve a consumir. El outbox git/suite de trenes terminales se
marca `cancelado` en esa misma transacción, conservando payload y resultados.
Nunca se cancela su feedback. Un resultado anterior no puede contaminar una
generación nueva.

La tabla siguiente es la política completa de `juicios.POLITICA`.
«Suite nueva» conserva exactamente C, cierra la generación anterior como
`ABSUELTO` y crea otra en `Armado`, con identidad nueva de rama/recibos:
readmisión, publicación, CI verde y **otra suite completa** antes de certificar.
Nunca interpreta una absolución como verde. «Expulsar» emite `devolver` de
U14 (comentario con juicio/motivo y `failure` sobre el head entregado), cierra
el tren `DEVUELTO` y congela sólo sus sobrevivientes sobre B en otra
generación, con `pending`, CI y suite completa. Los miembros ya rechazados
no pasan a esa generación ni reciben otro `pending`; conservan su caducidad.
En un conflicto sin C todavía,
el feedback conserva B y deja C vacío; no inventa una unidad de prueba.
«Abortar» cierra `ABORTADO`, libera sus entregas con custodia intacta y sin
rojo, y las readmite por las reglas normales desde el tick siguiente.

| Motivo de pausa | Detalle | Decisiones admitidas y efecto |
|---|---|---|
| `etapa-1` | vacío | `reanudar`: levantar la pausa del repo; siguen vigentes los permisos operativos externos para cada suite. |
| `certificado-sin-base` | vacío | `abortar`: no se puede promover sin B conocido. |
| `escritor-externo` | vacío | `reanudar`: levantar la pausa del tren `REARMAR`; sus entregas ya liberadas se readmiten sobre el main observado de nuevo, en otro tren. Nunca sobrescribe main ni marca el C anterior aterrizado. |
| `infraestructura` | `lanzador-rechazo` / `corrida-interrumpida` / `salida-sin-recibo` | El lanzador salió 64, Jale confirmó la interrupción con 125 y `PUEUE_INTERRUPTED=1` sin recibo, o Jale guardó otra salida entera distinta de 75 sin recibo. La pausa conserva la ruta del log y cierra el efecto. Con `lanzador-rechazo` o `corrida-interrumpida`, `reanudar` lanza otro intento con registro y clave nuevos. Con `salida-sin-recibo`, `reanudar` reengancha el mismo registro con la misma clave y conserva la salida anterior. Estas pausas no gastan reanudaciones por 75 ni presupuesto de diagnóstico. `abortar` termina el tren y devuelve las entregas a la fila sin feedback de producto. |
| `infraestructura` | `checkout-operador:<estado>` | El tick no pudo obtener el SHA o verificar su snapshot. `reanudar` permite reintentar y `abortar` termina el tren. El tick conserva cualquier pausa vigente. |
| `infraestructura` | `tres-reanudaciones-agotadas` | `reanudar`: un intento adicional del comando guardado, con contador/clave nuevos y sin certificado; otro 75 vuelve a pausar. `absolver`: abandonar el diagnóstico o reenganche anterior y exigir CI y suite completa nuevos sobre el mismo C, incluso si el diagnóstico agotó D5. `abortar`: liberar entregas sin rojo. |
| `infraestructura` | `ci-sin-resultado` | `reanudar`: solicitar CI sobre el mismo C con ref/clave nuevas y reiniciar los 60 minutos; `abortar`: liberar entregas sin rojo. No admite `absolver`. |
| `rama-tren-ocupada` | vacío | `reanudar`: reintentar publicar el C guardado, sin forzar ni reemplazar la rama ajena; si sigue ocupada vuelve a pausar. `abortar`. |
| `conflicto-en-cadena` | vacío; PR/archivos en la pausa | `expulsar`: sólo el PR en conflicto; sobrevivientes con suite completa. `abortar`. |
| `candidato-no-corresponde` | vacío | `reanudar`: reintentar el armado sobre la B guardada, con clave nueva; no certificar el candidato rechazado. `abortar`. |
| `head-cambiado-en-tren` | vacío | `expulsar`: un miembro del tren; los sobrevivientes pasan de nuevo por admisión y suite completa. `abortar`. |
| `promocion-rechazada` | vacío | `reanudar`: un intento CAS adicional, con el certificado guardado y nueva readmisión/reconciliación; no vuelve a correr una suite ya verde. Otro rechazo vuelve a pausar. `abortar`. |
| `necesita-juicio` | `incompleto-invalido`, `resultado-desconocido`, `verde-incoherente` | `absolver`: suite nueva sobre C. `expulsar`: devolver un miembro. `abortar`. |
| `necesita-juicio` | `subconjunto-no-corresponde`, `conflicto-en-subconjunto`, `repeticion-no-corresponde`, `origen-distinto` | `absolver`, `expulsar`, `abortar`, con los mismos efectos. |
| `necesita-juicio` | `presupuesto-agotado-en-reanudacion`, `suite-incompleta`, `tamano-de-lote-no-soportado`, `sin-costo-de-suite-sana` | `absolver`, `expulsar`, `abortar`, con los mismos efectos. |
| `necesita-juicio` | `sin-duracion:<unidad>`, `presupuesto-agotado:<unidad>`, `confirmacion-candidato:<unidad>`, `definicion-distinta:<unidad>`, `confirmacion-base:<unidad>`, `reproduccion-incierta:<unidad>` | `absolver`, `expulsar`, `abortar`, con los mismos efectos. |
| `necesita-juicio` | `intermitente-reincidente:<unidad>` | La unidad ya tiene dos absoluciones automáticas en siete días para este repo. `absolver` exige una suite nueva sobre C. También admite `expulsar` y `abortar`. |
| `manual` | motivo humano | Sólo `pausa --levantar`: continuar desde la fase guardada. `juicio` no la levanta. |

El tick activo puede absolver una unidad que pasa dos veces en C y dos veces en
B (caso 1), o que falla dos veces con una firma estable en C y al menos una vez
con esa firma en B (caso 2). En ambos casos exige dos intentos observados en
cada commit y la misma definición. Para el caso 1, el recibo debe traer la
identidad de cada caso que falló en la suite y su resultado en cada repetición
de C, con un pase para cada caso en los dos intentos. Para el caso 2, el
adaptador debe declarar que la firma del recibo distingue fallos distintos del
mismo caso. Hoy ningún adaptador habilita estos casos. Los recibos de Hasito y
Jale carecen de la evidencia de pases por caso, por lo que se conserva
`confirmacion-candidato`. Sus firmas también pueden coincidir para errores
distintos del mismo caso, por lo que se conserva `confirmacion-base` con B mixto.

La absolución requiere una suite sin etapas pendientes,
un máximo de dos unidades fallidas y que todas sean intermitentes que cumplen
estas reglas o fallos preexistentes confirmados. Una unidad que sólo identifica
el check, o cuya firma está vacía o sólo contiene un código de salida, conserva
su pausa de diagnóstico.

El tick une los diffs propios de los heads congelados de cada PR, desde su
ancestro común único con B, con las rutas del diff de B a C sin detección de
renombres. Los primeros conservan los cambios que la mezcla absorbió. El
segundo incluye las rutas donde la mezcla aplicó los cambios. Para cada PR,
el tick también compara las rutas de su diff propio con los borrados entre
su ancestro común y B, sin detección de renombres. Si B borró o movió una
ruta que el PR toca, el tick deja el inventario sin acreditar y conserva la
pausa de diagnóstico. Los archivos que el PR añade desde su ancestro común
quedan fuera de esta comprobación. El diagnóstico también conserva la pausa
si algún diff falla, si algún ancestro común falta o es múltiple, o si una
ruta del inventario coincide con el archivo de una unidad fallida. La
comparación admite rutas relativas a un paquete.

Al certificar C con la suite ya corrida, el ledger escribe un evento
`intermitente-absuelto` por unidad absuelta. Guarda repo, tren, B, C, unidad,
firma, caso y veredictos de C y B. Estos eventos fijan el límite de dos
absoluciones de la misma unidad en siete días. El certificado sigue el camino
habitual de pre-aterrizaje y promoción.

Las decisiones admitidas se filtran también por la fase/evidencia guardada:
no se reanuda infraestructura sin comando durable o fuera del presupuesto
D5 de diagnóstico, ni promoción sin
`Promoviendo`. Sólo se absuelve en `Corriendo` o `Diagnosticando`, con
candidato y sin miembros rechazados en el snapshot; el CLI sale con 2 sin
registrar la decisión y el expediente la omite si no cumple esas condiciones.
Una intervención nunca cambia
heads, inventa una base o evita CI verde/suite verde. `absolver` y `abortar`
no publican rojo; los miembros de una generación nueva reciben `pending`.

`estado --juicio` añade `juicio` al JSON habitual: identidad, motivo/detalle,
tren y fase, miembros con PR/head, resultados durables de suite/repetición y
las decisiones admitidas con cada comando exacto (uno por PR cuando aplica).
Incluye todas las decisiones registradas del repo y su estado, motivo de
rechazo e instante de consumo. El expediente manual muestra también el tren
que quedó detenido, aunque la orden manual tenga alcance de repo.
No requiere red y conserva la lectura consistente de U13 bajo SQLite.

Las pruebas U15 invocan el CLI real y avanzan con `tick`, con HOME/XDG
temporales canónicos, origen git local y los transportes falsos U13/U14.
Cubren verde/rojo después de absolver, expulsión y sobrevivientes, aborto y
custodia, escritor externo, decisiones inválidas y obsoletas, cada reintento
operativo, pausa manual durante recuperación y muertes reales antes/después
del consumo. Los injertos añaden expulsión con dos heads caducados, rechazo
de absolución por snapshot/fase, salida de infraestructura con D5 agotado,
sombra con decisión pendiente, cobertura de cada motivo/detalle del reductor
y muerte durante el reintento de promoción con ACK perdido sobre git real
(un aterrizaje y una sola suite). `tests/remoto-u15-injerto.sh` ejecuta las
tres regresiones contra `185e7a46` y exige sus fallos antes de probar el head
actual. Se incluyen en `tests/remoto.sh`; no se ejecutan en la máquina
que sólo edita código. No se activó ningún repo. Siguen pendientes D2/D8
y el contrato del lanzador real. La salida de CI rojo sobre C queda cubierta
por U16.


## CI sobre el candidato y salida del carril (U16)

`GitHubGit.ci` devuelve `ResultadoCI(estado, rojos)`: tres valores y los
nombres de checks/statuses rojos, ordenados y sin duplicados. La admisión
sigue exigiendo verde en cada head; la lectura del candidato consulta su C
exacto y liga el resultado a `tren_id` y `ci_recibo=github:<C>`. Un rojo de
otro tren o de otro C no produce partición ni devolución. Al reanudar, el
recibo incluye además `:tren/<id>-ci-<n>` y sólo acepta esa solicitud.

| Fuente | Conclusión / estado | Lectura |
|---|---|---|
| Check terminado | `success`, `skipped`, `neutral` | `verde` |
| Check terminado (salvo la excepción Actions siguiente) | `failure`, `timed_out` | `rojo` |
| Actions terminado en `failure` | falla `Set up job` o `Set up runner` antes de ejecutar un paso del workflow | `pendiente` |
| Actions terminado en `failure` | pasos no disponibles, incompletos o job no verificable | `pendiente` |
| Actions terminado en `failure` | el workflow llegó a ejecutar pasos y no hubo ese fallo previo de preparación | `rojo` |
| Check terminado | `cancelled`, `action_required`, `startup_failure`, `stale` | `pendiente` |
| Check no terminado | cualquiera, incluido `success` prematuro | `pendiente` |
| Check terminado | conclusión ausente o desconocida | `pendiente` |
| Commit status | `success` | `verde` |
| Commit status | `failure` | `rojo` |
| Commit status | `error`, `pending`, estado desconocido | `pendiente` |

En modo activo, `success` acredita aprobación; `skipped` y `neutral` son
conclusiones aceptadas por GitHub, incluso si el check figura en
`checks_esperados`. La configuración exige presencia: un workflow que nunca
emite su check sigue pendiente. `failure` y `timed_out` informan el resultado
negativo observado, sin afirmar una causa de producto D5. Una cancelación
humana o por concurrencia, una intervención requerida, un fallo de arranque,
evidencia vencida o un status `error` no acreditan un fallo del cambio:
esperan hasta el plazo de infraestructura. Valores desconocidos también
esperan sin inventar éxito ni culpa.

Para un `failure` de la app `github-actions`, se hace una sola consulta extra
al [job de Actions](https://docs.github.com/en/rest/actions/workflow-jobs#get-a-job-for-a-workflow-run),
no por cada check verde o pendiente. Se toma el id del job de `details_url`
(`https://github.com/<repo>/actions/runs/<run>/job/<job>`), sin suponer que
coincida con el id del check. Su `head_sha` y `check_run_url` deben corresponder
al C y al check consultados. Los pasos se recorren por `number`: un fallo de
`Set up job` o `Set up runner` antes de cualquier paso del workflow no
saltado conserva `pendiente`, incluso si aparecen `failure` sintéticos más
adelante (p. ej. tras checkout e instalación saltados). `Complete job` no
cuenta como ejecución del workflow. Un fallo de un paso del workflow después
de una preparación correcta sigue rojo. Si falla la consulta, la URL no
identifica un job verificable o no hay pasos terminados suficientes, se
espera al plazo de infraestructura: no se inventa rojo. Los checks de otras
apps y `timed_out` conservan la tabla anterior. La sombra y `--seco` no
consultan jobs.

Se conservan todas las páginas. Sólo decide el check-run con mayor id por
nombre y el status más reciente por contexto. Un rojo explícito prima sobre
pendientes y esperados ausentes; un faltante sólo impide **verde**. Verde
exige que todos los checks/statuses vigentes sean aceptables y que existan
todos los esperados. Sin observaciones se espera, salvo CI declarado vacío
(`checks_esperados: []`). `pao-tren/resultado` se excluye tanto en checks
como en statuses; sólo la sombra de doble camino conserva su inclusión.
La sombra y `tick --seco` mantienen su lectura histórica: únicamente
`success` es verde, cualquier otra conclusión retiene.

La tabla `SALIDAS_CI` y `avanzar_ci` modelan la salida de `EsperandoCI`:

- **Verde:** mantiene el camino existente de suite y promoción de C.
- **Rojo con más de un miembro:** cierra el tren `PARTIDO`, sin comentario,
  status rojo, consumo de suite especulativa ni escritura en main. En el mismo snapshot libera todas
  las entregas con `partida=True` y `solo=True`. Los ticks posteriores forman
  trenes individuales en su orden FIFO, cada uno sobre el main entonces
  vigente, con candidato, CI y suite propios. No se atribuye el fallo del lote.
- **Rojo con un miembro:** cierra `DEVUELTO`, resuelve esa entrega y deja
  libre el carril inmediatamente. Un efecto `devolver` de U14 publica un
  comentario marcado con lote, B, C, head y `ci_rojos`, seguido de `failure`
  enlazado al comentario. No inventa unidad ni comando de reproducción de
  Hasito para un fallo de GitHub. Otra entrega puede formar tren en el
  siguiente tick, aun si queda feedback pendiente.
- **Pendiente:** a los 59 minutos sigue esperando; desde los 60 minutos
  pausa `infraestructura` / `ci-sin-resultado`, sin rojo al dueño. El plazo
  usa `observacion.ahora` desde la transición a `EsperandoCI`, aunque falten
  checks o la admisión esté retenida, y se evalúa cuando el tick puede
  observar. Un fallo al leer el CI cuenta como pendiente. Una caída que
  impide leer `main` hace fallar el tick hasta que vuelva GitHub; el primer
  tick que pueda observar evalúa el plazo vencido. Un snapshot anterior sin
  `desde` comienza a contar en el primer tick de U16.
  Un verde observado sin suite confirmada **no detiene el plazo**: si el CI
  vuelve a pendiente se cuenta desde la entrada original. Una pausa manual
  tampoco descuenta tiempo. Un recibo durable de suite sí permite recuperar
  su lanzamiento con el CI verde guardado y salir de `EsperandoCI`, aunque
  la lectura fresca haya vuelto a pendiente.

`reanudar` conserva el tren y C, reinicia `desde` al consumir el juicio y
emite `solicitar-ci` con contador/clave nuevos. Publica una ref
`tren/<id>-ci-<n>` sobre ese mismo C: repetir la publicación de la ref
original sería un no-op de git y no pediría otra ejecución. Una muerte
reutiliza exactamente la ref del contador guardado. Se sigue leyendo el CI
por C y se exige verde antes de consumir la suite. Para esta nueva solicitud se
consultan también todas las páginas de
[check suites del commit](https://docs.github.com/en/rest/checks/suites#list-check-suites-for-a-git-reference):
la API garantiza `head_branch`, `head_sha` e `id` en cada suite, mientras el
[check-run](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
sólo garantiza `check_suite.id`. Se piden los checks con `filter=all` (el
default `latest` filtra por `completed_at`), se cruzan por ese id y se exigen C y la ref
publicada exactos **antes** de elegir el mayor id por nombre. Un check de la
ref anterior, aunque termine después de reanudar, no sirve. Hasta observar
checks de la solicitud vigente, la lectura es pendiente y corre el plazo
nuevo, incluso con `checks_esperados: []`. Los commit statuses no informan
la ref solicitante: al reanudar no se consumen, ni como verde ni como rojo.
Un proveedor que sólo publique statuses o no exponga la suite con esa ref
queda pendiente hasta el plazo; debe publicar checks ligados a la solicitud
para habilitar la suite. El recibo incluye la ref también en el buzón de
recuperación; un recibo anterior no autoriza una suite de la nueva solicitud.
No relanza automáticamente checks fallidos.
`abortar` conserva el comportamiento U15: cierra `ABORTADO`, libera
la custodia sin rojo y permite readmisión desde el tick siguiente.

**No aplica `absolver` a `ci-sin-resultado`.** En `juicios.POLITICA` esa fila
sólo contiene `reanudar -> ci-nuevo` y `abortar -> abortar`. `suite-nueva`,
la operación de absolución U15, exige `Corriendo` o `Diagnosticando`: aquí
no existe suite ni diagnóstico que absolver y omitir CI sería un certificado
falso. El validador exige `EsperandoCI` para `ci-nuevo`; el expediente sólo
ofrece las decisiones que pasan esa validación.

La marca `partida` pertenece al recibo `(repo, PR, head)` y resiste cada
readmisión, incluida una retención transitoria, conservando `recibida` y la
primera `admitida`. Dura hasta resolver la entrega: formar un individual no
la consume; si se aborta o rearma, vuelve a formar un tren de uno. Un head nuevo
caduca la entrega anterior por las reglas existentes y **no hereda** la
marca: no es la entrega que participó en el lote rojo y tendrá su propio
APTO/dirigida/CI. Los atributos sensible y alto riesgo siguen imponiendo
viaje individual por su propia política.

El reductor ignora un rojo sin nombres válidos y lo trata como pendiente;
no parte ni devuelve sin evidencia nominada. El escritor de feedback también
rechaza ese payload antes de consultar o publicar en GitHub.

Snapshot, devoluciones a la fila, cierre y efectos se guardan en el mismo
COMMIT de U13/U15. La recuperación del comentario usa el marcador U14; en
esta devolución por CI se reconcilia además el último status del contexto
contra el payload y URL antes de repetir un POST tras perder su ACK. Las
claves y el orden por PR impiden adelantar un `pending` posterior al feedback
anterior incompleto. Los efectos de trabajo del tren terminal se cancelan
sin cancelar su feedback. La extensión aditiva omite `partida=False` al
serializar: custodia y eventos `sombra`, `sombra-doble-camino` y
`admision-entrega` conservan su contrato. `tick --seco` usa la sombra sin
consumir el CI operativo ni ejecutar efectos.

`tests/remoto-u16.sh`, llamado por `tests/remoto.sh`, injerta las tres
regresiones de partición, devolución individual y plazo/reanudación sobre
`85138706`: exige tres fallos de aserción, cero errores, y conserva la
salida. Después, la suite completa comprueba el comportamiento corregido,
la tabla de conclusiones, la política exhaustiva de juicio, el aislamiento
de C, la marca por entrega y muertes reales entre COMMIT y drenado, después
del comentario y después del status. Todas las pruebas usan stdlib,
HOME/XDG temporales canónicos, git local y transportes falsos sin red.
`tests/remoto-u16-injerto.sh` exige siete regresiones de aserción sobre
`50cb5cda` (cancelación/infraestructura, skips esperados, rojo con faltantes,
partición tras aborto y rearme, rojo sin nombres en reductor y feedback), y
las ejecuta después sobre el código actual. La comparación contra `85138706`
exige igualdad de los eventos de sombra de doble camino y `--seco` fuera
de los campos de formación de lotes.
El lanzador remoto solapa los injertos históricos con la suite actual,
en procesos y temporales separados, para respetar el carril rápido de diez
minutos. Espera ambos resultados y conserva el log completo de los injertos;
un fallo en cualquiera de los dos caminos hace fallar la corrida.


## Admisión activa sobre el bare del tren (U17)

El tick activo observa la admisión en el mismo almacén con que arma y
promueve: `pao-tren` construye `GitTren(remoto, identidad=...)` y lo entrega como `almacen`
a `GitHubGit(config, activo=True, almacen=git)`. Antes de `merge-base` y
`merge-tree`, `GitHubGit.lector` prepara ese bare y comprueba con
`cat-file -e` la base y el head de cada PR. Sólo trae lo que falte: la base
por SHA y el head por `refs/pull/<n>/head`, con el mismo `traer` de U2 y el
mismo flock del carril. El C que acaba de aterrizar ya está en el bare,
porque allí se armó. El main que movió un escritor externo, y que Felix
reanudó, se trae por SHA en el tick siguiente.

Por qué el bare y no un fetch acotado en `config["git"]`: es un checkout de
uso humano. Un fetch allí escribe `FETCH_HEAD` y objetos sin ref, que su
propio `gc` puede podar, en un repositorio que el tren no serializa. El bare
ya es propiedad del tren, sólo se toca bajo el candado y ya contiene C y los
heads armados. Nada mueve `HEAD`, ramas ni el árbol de trabajo de `git`.

Un fallo al traer, un objeto que no coincide o un bare inválido dejan la
vista del PR incompleta. La entrega queda retenida por `observacion-incompleta`,
sin rojo, comentario ni status, y el tick siguiente reintenta. Los errores
no guardan argv ni stderr.

Los fetch de una observación comparten un presupuesto,
`PRESUPUESTO_TRANSPORTE_S` (120 s), sumado sobre toda la cola: cada fetch
recibe como plazo lo que quede. El primer fetch que falla, de la base o de un
head, da el remoto por caído: ninguna otra vista trae en ese tick, y las que
falten quedan incompletas. Así una caída cuesta a lo sumo un presupuesto por
tick bajo el candado del carril, no un timeout por PR. Un fetch que trae otro
SHA es propio de ese PR y no corta a los demás. El tick siguiente empieza con
el presupuesto entero.

Sólo el tick activo lee en paralelo, con hasta cuatro hilos, los datos de
los PR, sus archivos, evidencias y CI. Los cierres siguen el orden de las
entradas: `merge-base` y `merge-tree` usan el almacén con su presupuesto de
transporte compartido, y la última lectura del PR comprueba su vigencia
después de las demás lecturas de esa entrada.

La sombra, `entregar` y `tick --seco` no reciben almacén: un `GitHubGit` con
almacén fuera del tick activo es un error. Siguen leyendo `config["git"]`
sin fetch, y sus eventos `sombra`, `sombra-doble-camino` y `admision-entrega`
no cambian.

`tests/test_u17.py` usa como `config["git"]` un clon no bare, separado del
origen y del bare del tren, sincronizado una sola vez antes del primer tick.
Cada tick pasa por la construcción real de `pao-tren`; sólo se sustituye el
transporte de gh. Cubre dos lotes sucesivos, el main ajeno tras reanudar y
la falla al traer objetos. En los tres casos el checkout conserva `HEAD`,
ramas, árbol de trabajo y `FETCH_HEAD`, y nunca recibe C. `tests/remoto-u17.sh`,
llamado por `tests/remoto.sh`, exige que las tres regresiones fallen por
aserción sobre `0f7fff27` y pasen después. También compara los eventos de
sombra, `entregar` y `--seco` de ambos árboles fuera de la formación de lotes.

El checkout de esas pruebas tiene una rama propia, un índice distinto del
árbol y un archivo no versionado; todos sus bytes, `.git` incluido, quedan
iguales. Las pruebas de objetos cubren la base ausente cuyo fetch falla y se
recupera, la ref del PR adelantada respecto de la API (retiene sin leer otro
objeto, y la entrega nueva admite el head que sólo existe en esa ref), el
head ya presente que se lee sin consultar la ref mientras el armado la
vuelve a traer y no arma, y la composición productiva sin `GitTren`
inyectado. `tests/remoto-u17-injerto.sh` exige que las dos pruebas del
presupuesto fallen por aserción sobre `d0019396` y pasen después; usan
transporte y reloj falsos, sin esperas reales.

## Verificación previa al aterrizaje (U18)

El gancho es optativo y se declara en la configuración privada del repo:

```json
{
  "pre_aterrizaje": {
    "comando": ["/absolute/path/to/preflight", "--private-config", "/private/config.json"],
    "plazo_s": 30,
    "espera_max_s": 1800
  },
  "heads_autorizados": [
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    {"head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "ventana": {
      "inicio": "2026-12-01T10:00:00Z", "fin": "2026-12-01T10:30:00Z"
    }}
  ]
}
```

`comando` es argv sin shell; su primer elemento debe ser absoluto. El plazo
por llamada debe ser mayor que cero y como máximo 60 segundos. Todas las
invocaciones del gancho de un tick, tanto `verificar` como `cancelar` y para
todas las identidades pendientes, comparten **60 segundos de IO**: cada llamada
recibe como timeout el menor entre su plazo y el presupuesto restante; agotado
éste no se inicia otra llamada en ese tick. Este presupuesto corresponde al
gancho, no sustituye los plazos de los transportes git/GitHub ni de la suite.
`espera_max_s` es opcional (1.800 segundos por defecto), finito y mayor que cero;
limita la espera total entre ticks, independientemente del plazo por llamada.
Se usa el entorno del tick, sin variables que redirijan el almacén git; el gancho no
puede depender de su cwd. Configuración, ejecutable y credenciales pertenecen
al operador, fuera del árbol de C: un PR no elige el verificador que lo autoriza.
No se publican argv, stderr ni texto libre del gancho en eventos o feedback.

El tick escribe un JSON en stdin (sin argumentos añadidos):

```json
{"version":1,"accion":"verificar","id":"<identidad estable>",
 "repo":"owner/project","tren":"<id del lote>","B":"<SHA40>","C":"<SHA40>",
 "almacen_git":"/absolute/path/to/train.git",
 "prs":[{"pr":1,"head":"<SHA40>"}]}
```

`id` liga repo, lote, B y C y permanece igual al reconsultar o recuperar una
muerte. `prs` conserva el orden del lote y las cabezas exactas entregadas.
`almacen_git` es el bare del tren con B y C disponibles, para lecturas del
diff; el gancho no modifica sus objetos, refs ni configuración.
El gancho termina con código 0 y **un único objeto JSON** en stdout, sin
campos adicionales, con la versión e identidad recibidas:

```json
{"version":1,"id":"<la misma identidad>","resultado":"seguir"}
```

- `seguir`: autoriza **este intento** de push sobre B..C.
- `esperar`: no empuja ni consume un intento de CAS. El tick siguiente vuelve
  a llamar `verificar` con el mismo id; puede conservar una operación en curso.
  El vencimiento UTC se guarda en el diario **antes de la primera invocación**;
  las reconsultas y las muertes del tick no lo renuevan. Si vence, incluso durante
  una invocación que devuelve `seguir`, no se empuja: se cancela y se pausa
  `infraestructura` / `pre-aterrizaje:gancho-espera-vencida`, con `reanudar` y
  `abortar`, sin feedback rojo. Un registro anterior a este campo recibe su
  vencimiento al recuperarse para verificar por primera vez con esta versión.
- `pausar`: pausa `infraestructura`, con `reanudar` y `abortar` y sin rojo al
  dueño. `reanudar` exige otra verificación, con CI/suite ya certificados
  sobre el mismo C. No hay absolución que permita omitir el gancho.
- Código distinto de 0, timeout, versión/id/resultado desconocidos, JSON
  mal formado, claves duplicadas o stdout mayor que 16 KiB: misma pausa. Al agotar el plazo se
  mata el grupo del proceso; stderr se descarta. Nunca interpretar un error
  como permiso para empujar.

La llamada corre bajo el flock del carril, sólo en el efecto de promoción,
después del CI y la suite verdes sobre C y de comprobar su árbol y ancestros,
inmediatamente antes del push CAS. Después del gancho se relee el remoto y
el reloj UTC: si B cambió, su permiso no sirve; si el cambio ocurre después
de esa lectura, el lease sigue impidiendo el push sobre la otra base.
Un permiso no se guarda como certificado ni se reutiliza en otro tick.

Las autorizaciones históricas por SHA siguen válidas sin ventana. Una entrada
con ventana guarda ambos extremos junto a su head, exige UTC con `Z`, segundos
y `inicio < fin`; no admite duplicados. Sólo el observador activo incorpora
la ventana y fuerza viaje individual. Se comprueba incluso sin gancho: antes
de `inicio` espera, desde `inicio` y antes de `fin` permite comprobar el
aterrizaje, y desde `fin` pausa `infraestructura` / `pre-aterrizaje:ventana-vencida`.
Se usa la hora real previa al push, no la hora de apertura del tick: también
se comprueba después de esperar al gancho y como última lectura local antes
de entregar el control al push. El tick garantiza que esa lectura está dentro
de `[inicio, fin)`; **no garantiza que el remoto acepte la actualización antes
de `fin`**. La ventana puede vencer durante el transporte y el CAS aún puede
aterrizar C: el lease protege B, no la hora. Tras un aterrizaje acreditado se
recupera su aviso, sin cancelarlo ni emitir rojo. La garantía de hora de
aceptación requiere enforcement de la ventana en el destino; un margen local
sólo reduce la carrera. Una ventana vencida requiere que
el operador renueve la autorización del head y reanude; reanudar por sí solo
no amplía la ventana. Ningún motivo del gancho o de la ventana devuelve el PR
ni publica status `failure` o comentario de fallo.

### Operaciones pendientes y cancelación

Antes de invocar `verificar` se guarda en SQLite un diario privado con el
request, argv y plazo. Una muerte entre el gancho y el CAS deja ese registro
pendiente: al recuperar se reconcilia main; si sigue B se vuelve a verificar,
incluso si la ejecución muerta había obtenido `seguir`. Si C ya aterrizó se
cierra el diario y se recupera el aviso de aterrizaje, sin llamar al gancho
ni cancelar. El ACK de promoción guardado tampoco autoriza otro push.

Si no aterriza y hay una operación pendiente, se envía el mismo request con
`"accion":"cancelar"`; la única respuesta válida es código 0 y
`{"version":1,"id":"<la misma identidad>","resultado":"cancelado"}`.
Se cancela al pausar, abortar, caducar/perder admisión, salir de la ventana,
cambiar B, cambiar o retirar el gancho, ante un fallo del gancho o un CAS
rechazado. `esperar` conserva la operación mientras el lote y su admisión,
base, ventana y configuración sigan vigentes. La cancelación utiliza el
comando y plazo guardados, aunque ya no estén en la configuración.

Antes de cancelar se reconcilia: sólo se cancela con no aterrizaje acreditado;
si main es ajeno se trae y comprueba que no contiene C. Si lo contiene, se
cierra el diario sin cancelar. Una lectura incierta o una cancelación fallida
retiene en el diario la **obligación durable de cancelar**, junto con el último
error. Esa obligación es la barrera de todo el repo: ningún tren puede hacer
push de promoción hasta confirmar `cancelado` o acreditar que C ya aterrizó.
No crea una pausa del carril. `reanudar` no la borra; armado, CI, suite,
decisiones y feedback de los demás trenes siguen avanzando hasta antes del
push. Se reintenta en cada tick y, al resolverse, el carril sigue sin otra
decisión humana. Esto también vale para una lectura incierta después del CAS:
al acreditar C se cierra el diario y sale su aviso de éxito sin cancelar.
Si el push no aterrizó y la cancelación falla, al confirmarla en otro tick se
vuelve a verificar el gancho antes de reintentar el push, sin decisión humana.
Una cancelación fallida no sustituye la lectura ni el motivo original de un
gancho o una ventana que sí exigían pausa. `pao-tren estado` muestra las
obligaciones pendientes en
`cancelaciones_pre_aterrizaje`, con id, tren, B, C y `ultimo_error`, sin argv ni
salida privada del gancho. Se hace **a lo sumo una tentativa de cancelación por id
y tick**, incluida su reconciliación. La obligación se guarda al decidir cancelar,
antes de aplicar ese límite o el presupuesto de IO; diferir la tentativa no la
borra, y volver a verificar el mismo id tampoco puede borrarla. Sólo la cierra
un `cancelado` confirmado o C acreditado como aterrizado. El fallo de una no impide intentar las
otras dentro del presupuesto compartido. La limpieza pendiente no impide
consumir decisiones ni drenar feedback de ningún tren. Antes de aplicar
`abortar` a un tren en promoción se reconcilia sin push: si C aterrizó (también
si main ya lo contiene), se registra ATERRIZADO, se emite su éxito y se rechaza
el aborto por pausa resuelta. Si la lectura sigue incierta, el aborto queda
pendiente y se reintenta en otro tick, sin cancelar ni cerrar como ABORTADO.
La política del prefijo `pre-aterrizaje:<unidad>` sólo ofrece `reanudar` y
`abortar`; estas pausas del gancho o la ventana conservan su decisión humana.
Por compatibilidad, una pausa `cancelacion-pre-aterrizaje` guardada por una
versión anterior se levanta con `reanudar`, conservando la obligación del
diario. Los ticks nuevos no crean ni reponen esa pausa de limpieza.
Los registros se marcan cerrados, no se borran. No se promete cancelar mientras
no haya otro tick: el operador debe recuperar el diario si retira el servicio.

El implementador del gancho debe hacer `verificar` y `cancelar` idempotentes
por id, con estado y evidencia durables **antes** de cada efecto remoto.
Debe serializar esas operaciones por id, incluso si un proceso sobrevive a la
muerte de su tick; respetar el plazo y no dejar trabajadores fuera del grupo.
`cancelar` es seguro aunque la solicitud sólo se registrara y no llegara a
escribir un efecto. Debe comprobar también la identidad del destino remoto y
que C no haya aterrizado antes de deshacerlo; ante incertidumbre devuelve
error. Una operación que no puede garantizar eso requiere intervención, nunca
un `cancelado` inventado. La implementación propia del repo conserva su
evidencia detallada y cualquier dato privado fuera del diario público de eventos.

Por ejemplo, la primera llamada `verificar(id)` registra durablemente una
solicitud de preparación, la pide idempotentemente y consulta su estado. Si
todavía no está lista devuelve `esperar` inmediatamente. Los ticks siguientes
consultan la misma solicitud por el mismo id y vuelven a devolver `esperar`
hasta que esté lista; entonces responden `seguir` tras validar B..C de nuevo.
No se mantiene un proceso vivo durante toda la espera. El vencimiento durable
del tick limita ese ciclo aunque cada consulta tarde poco; el guion también
debe acotar y conservar durablemente la vida de su operación remota. Si se
abandona el intento, `cancelar(id)` lo deshace con seguridad. Una reanudación
tras cancelación confirmada abre otro plazo total, pero vuelve a verificar:
el mismo id no puede reutilizar un permiso ni eludir la idempotencia.

Sin `pre_aterrizaje` y sin ventanas se conserva el comportamiento previo.
Sombra, `entregar` y `tick --seco` no ejecutan el gancho ni su cancelación,
conservan la interpretación histórica de `heads_autorizados` (sólo SHA
literales), no importan ventanas a sus vistas y mantienen los mismos eventos
byte a byte.
U18 no cambia ninguna inscripción ni activa repos.

Para activar un repo que requiere verificación previa se necesita el gancho
configurado y un guion propio que cumpla este contrato. Ese guion y su prueba
de contrato sobre el entorno real pertenecen al repo consumidor; PAO provee
y prueba el protocolo genérico.

`tests/remoto-u18.sh`, llamado desde `tests/remoto.sh`, exige seis regresiones
por aserción sobre `a1d24ec1` y su éxito después. Usa ticks, un origen git real,
un guion falso y una muerte de proceso entre verificación y push. También
compara los eventos de sombra, entrega y `--seco` de ambos árboles fuera
de la formación de lotes, con un gancho declarado que no debe ejecutarse.
La suite cubre cancelación,
recuperación del push con ACK perdido, timeout y expiración durante el gancho.
`tests/remoto-u18-injerto.sh` añade seis regresiones por aserción contra
`c06a9c70`: aborto y feedback con limpieza persistentemente fallida,
cancelación por id/tick con reloj y transporte falsos, espera durable,
configuración productiva con el nuevo plazo total y ventana individual
(los otros PR siguen admitidos), y lectura final del reloj local. La ventana
individual ya existía en esa base; su regresión de composición falla antes
porque el contrato anterior rechaza `espera_max_s`. Se prueba además el límite
documentado con un transporte que acepta el push cuando ya venció la ventana,
y el presupuesto compartido entre identidades y entre verificar/cancelar.
`tests/remoto-u18-r1.sh` exige tres regresiones por aserción contra `4498b3dd`:
aborto y reanudación con cancelación fallida dejando avanzar armado/CI/suite,
reanudación del mismo tren sin saltar la barrera, y aborto tras recuperar una
lectura incierta del CAS sin perder el éxito. La suite comprueba además el
aborto que espera acreditación y C contenido en un main posterior.

## Adaptador de Jale (U9)

La configuración privada por repo admite `"adaptador": "jale"`, `"lanzador"`
(ruta absoluta a `scripts/test-hosts/run-remote.sh`), `"host"` (por defecto
`"auto"`) y `"modos"` (por defecto `["contract", "e2e"]`). La configuración
actual de Hasito con `lanzador_hasito` sigue siendo válida. Esta selección no
inscribe ni activa repositorios.

`"prelanzar": true` adelanta sólo el primer modo configurado de Jale al tick
que publica C y pide su CI. La clave es booleana, vale `false` por omisión y
se lee en cada tick. Apagarla surte efecto en el tick siguiente sin instalar.

### Checkout del operador

`checkout_operador` es un campo opcional del objeto de configuración privada
del repositorio. Contiene la ruta absoluta a la raíz de un checkout del
repositorio de producto. Al configurarlo, el tren ejecuta el lanzador
(`lanzador` o `lanzador_hasito`) y cada ruta absoluta de
`pre_aterrizaje.comando` situada dentro del checkout desde su ruta equivalente
en un snapshot. Esto incluye el primer elemento del comando. El intérprete
del sistema y las configuraciones privadas externas conservan sus rutas.
Al omitir el campo se ejecutan las rutas configuradas directamente.

Para prelanzar, iniciar, reanudar o repetir trabajo de suite, el tick lee el SHA de
`refs/heads/main` del remoto explícito del tren. Para el gancho usa el mismo SHA
que la reconciliación previa acreditó como base del CAS. Si falta el objeto,
el tren lo trae a su almacén bare y lo transfiere desde ahí al repositorio del
operador, bajo `refs/pao-tren/operador/<SHA>`. La elección del SHA y la
transferencia local son independientes de los remotos del checkout del
operador. Jale conserva la comprobación de `origin/main` de su lanzador. Si
entra un cambio del lanzador tras fijar el SHA, el 64 pausa la vía en
`lanzador-rechazo` y el nuevo intento toma otra lectura de `main`.

El tren reserva en el ledger la ruta del worktree desacoplado por SHA antes
de crearlo. Lo marca completo después de comprobar el HEAD, el árbol limpio
y el destino de cada ruta mapeada, incluidos sus symlinks. Si la creación
queda a medias, el próximo intento recupera la ruta reservada. Una ruta
ocupada sin reserva produce la pausa `checkout-operador:ruta-ocupada` y el
ocupante se conserva. Cada corrida guarda su argv antes de ejecutar. El
reenganche y la recuperación usan el argv guardado, también para registros
anteriores a los snapshots. Tras un 75 de Hasito, un intento nuevo valida
los argumentos contra el registro anterior y usa el lanzador que permite
la configuración vigente. El diario del gancho guarda el comando
verificado para reconsultarlo o cancelarlo.

Al final de cada tick activo, la limpieza conserva el snapshot más reciente
del repositorio, los que usan los trenes no terminales y los de los diarios de
pre-aterrizaje pendientes. Tras el cierre de un tren, conserva los snapshots
con lanzamientos todavía activos. Si falta un recibo terminal y no se puede
determinar si el proceso sigue vivo, los conserva 24 horas desde que el tren
quedó terminal. Quita los worktrees limpios restantes con
`git worktree remove`; si el directorio de un snapshot ya no existe, quita
sólo su registro. No corre `git worktree prune`: el repositorio del operador
puede ser compartido y la poda borraría registros de worktrees ajenos. Los
worktrees sucios se conservan para inspección. Si el SHA o el snapshot fallan, el
efecto y la pausa `infraestructura/checkout-operador:<estado>` se registran
juntos, salvo al prelanzar, cuando se registra un aviso. `reanudar` vuelve a
intentar el efecto y `abortar` aplica el cierre habitual del tren.

Un push a `main` de Jale despliega a producción y aplica migraciones, así que
con `"adaptador": "jale"` el bloque `pre_aterrizaje` (U18) es obligatorio. Sin
él, la promoción pausa `infraestructura` / `pre-aterrizaje:configuracion-ausente`,
sin push ni rojo. El tick sigue limpiando el diario, drenando feedback y
consumiendo decisiones: retirar el bloque cancela la operación pendiente con
el comando guardado, como en U18, y detiene el carril antes del push.

Jale corre ambos modos sobre C sin filtros y valida `jale-recibo@1`: SHA,
modo, selección, firmas, orden, unicidad, cobertura y código de salida.
Cada fallo conserva su firma sin declararlo preexistente; la atribución
requiere dos observaciones sobre C y dos sobre B. Las repeticiones usan la
ruta exacta como único filtro, guardan duración y versiones del entorno en
cada intento y reutilizan la evidencia de B por `(B, modo, unidad)`. El
adaptador publica los subconjuntos antes de lanzarlos con una ref estable por
tren y commit; los registros de repetición pertenecen al tren y la solicitud,
y la caché de B guarda por intento sólo punteros a recibos válidos sin 75. El
contrato identifica la definición por modo y ruta; no acredita igualdad del
contenido del archivo entre commits. En e2e, un recibo con código 0 puede
traer unidades `falla` cuando el juicio contra la base de main las dio por
compartidas; el adaptador las trata como fallos que la atribución repite. En
contract, una `falla` exige un código distinto de 0.

Con `prelanzar` habilitado, el primer modo empieza al pedir el CI. Cuando
`iniciar-suite` observa CI verde y miembros admitidos, confirma el registro
especulativo, recoge esa corrida y sigue con los demás modos. La confirmación
es durable y permite recuperar el resultado tras una caída del tick.
Una salida 75 que se produzca antes del verde ocupa la primera reanudación
cuando `iniciar-suite` la consume.

Si `solicitar-ci` se reintenta tras la muerte de un tick, vuelve a prelanzar
sobre el mismo registro, salvo que ese tick ya lea el CI en rojo.

Si falla el prelanzamiento, `prelanzamiento-fallido` registra el tren y
el error, y la solicitud de CI se completa. Si el CI sale rojo o un juicio
aborta el tren antes de confirmar la corrida, `suite-prelanzada-sin-leer`
registra la clave de la corrida (si llegó a escribirse) y si ya terminó.
La corrida sigue en el host hasta terminar y su resultado queda sin consumir.
SEN-7331 añadirá la cancelación.

Los logs y recibos durables quedan bajo `pao-tren/jale` en el directorio de
estado. Un coordinador desacoplado y un candado por corrida permiten recuperar
el resultado tras una caída del tick. Cada registro deriva una clave estable
de su ruta y la guarda en `iniciado.json` antes de lanzar el hijo con
`REMOTE_RUN_CLAVE`. Si muere también el coordinador sin salida durable, el
siguiente tick relanza el mismo argv con la clave guardada y recupera el
trabajo aceptado por el lanzador, sin otro envío. Cada intento, modo y tren
tiene su propio registro y clave. Los registros antiguos con `iniciado.json`
sin clave conservan el lanzamiento incierto y detienen su recuperación.
El coordinador permite como máximo tres invocaciones ante una salida 255
o 127. Usa la misma clave y guarda la última salida. Guarda el 125 sin
reintentar. Un 75 sigue la política de reanudación del tren. Un 64 cierra
el efecto y pausa `infraestructura` con detalle `lanzador-rechazo`. Si Jale
guarda un 125 con `PUEUE_INTERRUPTED=1` en `corrida.log` y sin recibo, la
pausa indica `corrida-interrumpida`. Las demás salidas enteras sin recibo
pausan con detalle `salida-sin-recibo`. Estas pausas conservan la ruta de
`coordinador.log` y también se aplican al recuperar un efecto ya fallido.
Con `lanzador-rechazo` o `corrida-interrumpida`, `reanudar` lanza otro
intento con registro y clave nuevos. Con `salida-sin-recibo`, `reanudar`
reengancha el mismo registro con la misma clave y conserva la salida
anterior. Si hay un checkout del operador, usa su lanzador vigente.
`abortar` libera las entregas sin rojo.
Hasito aplica la pausa del 64 y conserva su log y recibo. Estas pausas
no consumen reanudaciones de 75 ni presupuesto de diagnóstico. Tampoco
generan feedback de producto. Los recibos duplicados, malformados o
incoherentes dejan el efecto pendiente como error de infraestructura.
No hay cambio automático de host.

La regresión U9 y toda la suite del tren, dividida exhaustivamente entre dos
procesos aislados para el carril rápido, se verifican en el host de pruebas con
`bash fleet/tren/tests/remoto-u9.sh --completa`. El guion conserva la evidencia.
