# Entrega U4 puente: cuotas diurnas tras NVMe

Rama `t3/desk-pueue-u4-puente`, basada en `origin/main` `e056155b`.
Se implementó Hasito 3, Jale 2 y total 5, con dos `chain` de Jale.
`model.DAY_LIMITS` es la única tabla de cuotas; presupuesto total y máximo
carril Hasito se derivan de ella. Publicación, fallback, validación, admisión,
filtrado de pendientes y vista pública usan esos valores. Jale conserva los
carriles 1..2, la exclusividad y una sola `verify` de día. No cambia la noche,
la guardia de carga, la afinidad, el mutex ni los fsync.

`test_u4.DayBridgeTest` añade 10 casos: tres Hasito y dos Jale simultáneas,
dos `chain`, combinación `chain`/`verify`, vista y pendientes del carril 3,
política diurna válida y rechazo de las cuotas antiguas, carril 4 cerrado,
cuarta Hasito y sexta total (con claims nocturnas retenidas que dejan libre
un carril), exclusividad en ambas direcciones y segunda `verify` rechazada.
Se actualizan las expectativas anteriores de cuotas y se mueven los fixtures
de carriles cerrados al amanecer de 3 a 4; la prueba de alcance de reconciliación
usa Hasito 4/5 y Jale 3. El esquema de vista no tiene un JSON Schema separado;
el contrato permite enteros no negativos en `limits`, y las pruebas de vista
cubren explícitamente `max_lane=3` y `limits={jale:2,hasito:3}`.

## Verificación realizada y pendiente

Por indicación posterior de Felix, **RED y GREEN funcionales están pendientes
de desk**: está apagado por la migración. No se ejecutó ningún comando en desk,
no se desplegó y no se abrió PR. No hubo subagentes.

El primer intento, antes de conocer la incompatibilidad, fue en macOS de la
Mini (`20261111-224700-f899e4`): 8 errores de entorno por falta de `prctl`;
no constituye RED. No se montó ni se ejecutó un Linux en la Mini.
Después se ejecutaron sólo checks estáticos en la Mini:

```sh
REMOTE_RUN_HOST=mini ~/.local/bin/remote-run start --no-git --name dirigida-u4-puente -- 'bash fleet/deskpolicy/evidence/u4-macos.sh'
REMOTE_RUN_HOST=mini ~/.local/bin/remote-run wait -q 20261111-225054-45987d
```

Salida final, rc 0 (log completo `evidence/u4-macos.log`):

```text
Python 3.9 syntax + py_compile: OK (34 files)
ok: 12 citas; registro sin pstack (--registry-only)
ok: manifest válido (12 files, 24 skills, 7 retire, 4 plugins)
U4 macOS: syntax, py_compile, ShellCheck and pao check OK
```

## Comandos pendientes para cuando vuelva el desk

Ejecutar desde este worktree. RED conserva todo el código y las pruebas de
`e056155b` y superpone sólo las 10 pruebas U4. No cambia el checkout de trabajo.
El comando start imprime el ID que se pasa al wait correspondiente.

```sh
# Preparación local de un snapshot RED nuevo, sin ejecutar pruebas locales.
u4_red_dir=$(mktemp -d /tmp/pao-u4-red.XXXXXX)
git archive e056155b fleet | tar -x -C "$u4_red_dir"
cp fleet/deskpolicy/tests/test_u4.py "$u4_red_dir/fleet/deskpolicy/tests/test_u4.py"
# RED: se esperan fallos de aserción de las nuevas admisiones/vista, sin errores.
REMOTE_RUN_HOST=desk ~/.local/bin/remote-run start --dir "$u4_red_dir" --cd . --no-git --name dirigida-u4-puente --script "$PWD/fleet/deskpolicy/evidence/u4-red.sh"
REMOTE_RUN_HOST=desk ~/.local/bin/remote-run wait -q <ID_RED>

# GREEN: suite completa, pao check y ShellCheck del CI para los scripts del módulo.
REMOTE_RUN_HOST=desk ~/.local/bin/remote-run start --no-git --name dirigida-u4-puente -- 'bash fleet/deskpolicy/tests/desk-run.sh && shellcheck --severity=warning fleet/deskpolicy/deploy-desk.sh fleet/deskpolicy/withdraw-desk.sh fleet/deskpolicy/tests/desk-run.sh'
REMOTE_RUN_HOST=desk ~/.local/bin/remote-run wait -q <ID_GREEN>
```

## Consumidores leídos

Se leyó `git -C /Users/felixddhs/VSCODE/REPOS/comprador.-pueue-turno show
origin/main:scripts/mini/desk.py`. `lane_paths`, líneas 230–234, valida 1..12
y genera todos los locks hasta `max_lane`; `select`, líneas 389–394, lo toma
de la vista. No asume dos carriles y no necesita cambios. También se buscaron
esas suposiciones en `scripts/mini/run.py`, sin hallazgos.
`/Users/felixddhs/VSCODE/REPOS/Jale` no existe: no se pudo inspeccionar ese
consumidor. No se modificaron consumidores.

---

# Entrega U1, corrección para ronda 9

El objetivo de Felix se conserva. Quiere usar el desk para más corridas de
noche con el archivo de política PAO. Esta ronda corrige un solo hallazgo,
el P2 de la review automática de Codex en el PR #56 (comentario 4487647294).
El plazo de 10 s del tick del writer lanzaba `TimeoutError`, que es subclase
de `OSError`. Un manejador de recuperación podía quedarse con el plazo, y el
tick seguía sin plazo y con el mutex de admisión tomado.

El trabajador partió de `f03aaa4a7ec59955596cba5129693d5c6fa24f0c`, creó la
rama `t3/desk-politica-u1-r9` y trajo `origin/main` (`66a4999`) con el merge
`6b32ae4`, sin conflictos. El orquestador trajo después `origin/main`
(`9097336`) con el merge `6af9bc9`, también sin conflictos. Ninguno de los
dos merges cambia `fleet/deskpolicy` ni `fleet/contracts`. No hubo
descendientes ni collab.

## Commits y cambio

- `d366f20`: sólo `tests/test_r9.py`, antes del arreglo.
- `981b5a4`: sólo el mismo archivo. Evita depender de stdin en la sonda de
  `cerrar`.
- `6545f59`: excepción `WriterCycleTimeout(BaseException)` y captura por
  nombre sólo en el bucle del writer. Añade una frase en la línea 366 del
  contrato.
- `87ac7a5`: sólo `tests/test_r9.py`. La prueba de la CLI pública dispara el
  plazo dentro del `try` de `arm_valid`. La escribió el orquestador después
  de la primera corrida base, que la vio pasar sobre el código viejo.

El resto del runtime no cambia. Se conserva la línea observable de error
`pao/desk-error@1`, con `reason=census-unknown`,
`detail=writer-cycle-timeout` y `exit_code=75`. El bucle conserva su espera
de 20 s y arma un plazo nuevo en cada tick. `publicar --una-vez`, `admitir`,
`cerrar`, `reconciliar` y `estado` no arman la alarma. La prueba de la CLI
lo comprueba con un `setitimer` que falla si alguien lo llama.

## Manejador → commit → prueba

Las líneas de `engine.py`, `model.py` y `store.py` son iguales en `f03aaa4a`
y en el arreglo. Todos los casos son de `tests/test_r9.py::RoundNineTest`.
El commit de corrección de todas las filas es `6545f59`.

La columna «Antes» sale de la corrida `20261109-162108-a05f12`, con el
runtime y el contrato de `f03aaa4a`. La columna «Después» sale de la corrida
`20261109-162618-d79254`, sobre `87ac7a5`. Las 15 pruebas fallan antes por
aserción y pasan después.

«Absorbe» quiere decir que `engine.publish()` regresó con normalidad y
renovó la política (`returned True exception NoneType policy_changed True`).
«Jerarquía» quiere decir que `TimeoutError` ya llegaba al llamador y sólo
falla la aserción que exige una excepción fuera de `Exception`. En esos
manejadores la base no absorbía el plazo, y la prueba fija que un cambio
futuro tampoco pueda hacerlo.

| Manejador alcanzable desde el tick | Prueba (prefijo `test_` omitido) | Antes | Después |
| --- | --- | --- | --- |
| `engine.py:573`, reconcile_claim, OSError/ValueError | `reconcile_claim_oserror` | Absorbe | `WriterCycleTimeout`, política intacta |
| `store.py:252`, session_members, OSError/ValueError/IndexError | `session_members_outer_oserror` | Absorbe | `WriterCycleTimeout`, política intacta |
| `store.py:243`, session_members, FileNotFoundError/ProcessLookupError | `session_members_inner_missing_process` | Absorbe | `WriterCycleTimeout`, política intacta |
| `engine.py:595`, recover_receipt, Exception | `recover_receipt_exception` | Absorbe | `WriterCycleTimeout`, política intacta |
| `engine.py:554`, reconcile_receipt, FileNotFoundError | `reconcile_receipt_missing_file` | Absorbe | `WriterCycleTimeout`, política intacta |
| `engine.py:634`, arm_valid, OSError/ValueError | `arm_valid_oserror` | Absorbe | `WriterCycleTimeout`, política intacta |
| `engine.py:702`, publish, FileNotFoundError | `publish_missing_control` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `store.py:105`, open_file, BaseException con limpieza y raise | `open_file_baseexception_cleanup` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `store.py:173`, mutex, BlockingIOError | `mutex_blocking_io` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `store.py:188`, assert_held, BlockingIOError del probe | `assert_held_probe_blocking_io` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `store.py:196`, assert_held, BlockingIOError del fd heredado | `assert_held_owner_blocking_io` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `store.py:208`, held, BlockingIOError | `held_blocking_io` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `model.py:101`, strict_json, ValueError/UnicodeError/RecursionError | `strict_json_valueerror` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `model.py:55`, timestamp, ValueError | `timestamp_valueerror` | Jerarquía | `WriterCycleTimeout`, política intacta |
| `cli.py:345` (línea 341 en `f03aaa4a`), frontera del tick, con el plazo dentro de `arm_valid` | `public_cli_loop_aborts_then_publishes_with_new_deadline` | El primer tick imprime la política | Error en el tick 1 y política con revisión 2 en el tick 2 |

Salida de la prueba de la CLI pública sobre el arreglo:

```text
R9 CLI first {'detail': 'writer-cycle-timeout', 'exit_code': 75, 'reason': 'census-unknown', 'schema': 'pao/desk-error@1'}
R9 CLI second pao/desk-policy@1 2 night
R9 CLI tick=1 deadline=9.541984
R9 CLI tick=2 deadline=9.998682
```

La inyección usa `sys.settrace` para enviar SIGALRM al proceso justo dentro
del bloque protegido elegido, con `cycle_timeout()` real armado. No cambia
funciones del runtime ni añade costuras de entorno. Cada caso comprueba
también que la política queda idéntica en bytes, que el timer queda
desarmado, que el handler anterior se restaura y que otro proceso toma el
mutex con un `flock` no bloqueante. Después publica otra vez y exige que la
claim y el recibo queden cerrados, para comprobar la convergencia.

El caso público ejecuta el script CLI real con `publicar`, sin `--una-vez`,
y espera de verdad los 20 s entre ticks. En el primer tick sustituye una
vez `read_json` dentro de `arm_valid`, y la sustitución envía SIGALRM desde
ese `try`.

`read_policy` (`engine.py:263`), `legacy_veto` (`322/324/336`), las ramas de
`admit` (`418/449/456`), `close` (`525`) y `status` (`739`) no son
alcanzables desde `publish`. Tampoco lo son `execute`, `installation` ni
`load_sample` del harness. `DeskPolicy.close` sólo se llama desde
`cli.py:239` y `cli.py:321`, que son `execute` y el comando `cerrar`. El
handler exterior de `main` se incluye como frontera. El caso de la CLI
exige que el bucle continúe, así que una salida por esa frontera no
satisface la prueba.

## Limpieza y revisión estática

La búsqueda `rg -n 'except:|except BaseException|except Exception|finally:'
fleet/deskpolicy/*.py` no encuentra ningún `except` desnudo. Hay dos
`except BaseException`. El de `store.py:105` cierra el fd y relanza. El de
`installation.py:124` hace rollback y relanza, fuera del tick. En el tick,
`engine.py:595` sólo registra el diagnóstico del recibo, sin limpieza. Los
otros `except Exception` están en `execute`, fuera del tick. Los fds, los
temporales y el mutex del camino usan `finally` o `with`. Ninguna limpieza
del tick depende de `except Exception`.

## Corridas

Todas las pruebas corrieron en el desk con `REMOTE_RUN_HOST=desk
remote-run`, una corrida a la vez y bajo `taskset -c 0-1`. Cada guion
comprueba al empezar la carga de 1 minuto (menor que 9), el tiempo desde el
último arranque ajeno en `~/remote-run/runs` (120 s o más) y `MemAvailable`
(6 GiB o más), y sale con 75 si alguna falla. Los fixtures viven dentro del
mirror. La MacBook sólo editó archivos y calculó hashes.

| RunId | Nombre | rc | Resultado |
| --- | --- | --- | --- |
| `20261109-161810-5139eb` | `dirigida-desk-politica-u1-r9-base` | 1 | `test_r9.py` de `55766b0` sobre el runtime de `f03aaa4a`. 15 pruebas, 14 fallos, 0 errores. La prueba de la CLI pública pasó sobre el código viejo, porque enviaba SIGALRM antes de entrar a `arm_valid`. Gate con carga 1,31, 8313 s y 29,9 GiB. |
| `20261109-162108-a05f12` | `dirigida-desk-politica-u1-r9-base` | 0 | `test_r9.py` de `87ac7a5` sobre el runtime de `f03aaa4a`. 15 pruebas, 15 fallos por aserción, 0 errores, en 30,465 s. Gate con carga 0,37, 178 s y 29,8 GiB. |
| `20261109-162618-d79254` | `dirigida-desk-politica-u1-r9-fix` | 0 | Sobre `87ac7a5`, con el manifiesto de entradas verificado. `test_r9.py` 15 OK, `test_policy.py` 28 OK, `test_r2.py` 18 OK, `test_r3.py` 14 OK, `test_r4.py` 15 OK, `test_r5.py` 14 OK, `test_r6.py` 14 OK, `test_r7.py` 8 OK. `bin/pao check` con rc 0. Gate con carga 0,02, 308 s y 29,9 GiB. |

El rc 0 de la segunda corrida base quiere decir que todas las pruebas
fallaron por aserción y ninguna por error, que es lo que exige el guion.

El primer sondeo SSH del trabajador falló antes de leer la carga
(`Connection closed by UNKNOWN port 65535`, rc 255), y por la regla del
encargo no arrancó nada. `evidence/r9-blocked.log` lo conserva. El sondeo
posterior del orquestador respondió, y las tres corridas son suyas.

La suite completa (`fleet/deskpolicy/tests/desk-run.sh`) no corrió en esta
entrega. Corre una vez sobre el head que reciba APTO, antes del merge.

## Reproducción e integridad

Los logs completos quedan en `evidence/<runId>.log`. Sólo se retiran los
espacios finales. `evidence/r9-base.sh` es el guion de las dos corridas base
y `evidence/r9-fix.sh` el de la corrida del arreglo.

Para la base, el guion extrae `git archive f03aaa4a fleet` en
`.audit/r9-base` y superpone sólo el `tests/test_r9.py` del head. No
modifica ningún otro archivo de esa copia. La base imprime el hash
`d8084346baa0275af09f6e353b02671240dbba7b65eab1a87e616f9873e2edd9`, el mismo
que cierra la entrega de r8.

SHA-256 de los siete archivos de `store.SOURCE_FILES` del runtime corregido,
impreso por la corrida del arreglo:

```text
290239201ad899b06a60dea7a20e7f2c97e129a0ffb8e81da84cc7dd38b15324
```

`evidence/r9-inputs.sha256` fija el runtime, el contrato y `test_r9.py`. La
corrida del arreglo lo verifica antes de las pruebas (`R9 INSUMOS rc=0`).
Los cambios posteriores de `DELIVERY.md` y de `evidence/` no alteran esos
bytes. El hash r8 que se conserva más abajo es histórico.

No hay desviación del diseño elegido. No apareció ningún punto donde cortar
el tick deje un estado del que el tick siguiente no converja. Los 14 casos
de interrupción publican de nuevo y cierran la claim y el recibo.

## Fuera de alcance

No se desplegó y no se ejecutó `deploy-desk.sh` ni `withdraw-desk.sh`. No se
escribió en `/tmp/sen-test-slots` ni en el estado de comprador de ninguna
máquina. No se tocaron los consumidores de Jale y de Hasito ni
`fleet/hostgate`. No se reparó SSH, no se reinició infraestructura y no se
mataron procesos ajenos.

---

# Entrega U1, corrección para ronda 8

El objetivo sigue siendo ampliar las corridas nocturnas del desk con el
archivo de política PAO y conservar capacidad segura al volver a día.
Rama `feat/desk-politica-v1`, con fetch y seguimiento de origin. El head
inicial coincidió con `8fc1f9161fdf762a799b53370987339a944e8866`.
Se leyeron completos U1, r8, las correcciones r2 a r7, Sol A, el veredicto
y los dos informes r7. Nivel alto por contrato entre servicios. Se aplicó
Bug fix de felix-mode como trabajador. No hubo descendientes, PR,
despliegue ni cambios en Jale, Hasito o hostgate. La review final es de PAO.

## Hallazgos, commits y pruebas

`3475b11` registra las tres regresiones antes de los arreglos.
El baseline conserva el runtime y el contrato exactos de `8fc1f91` y
superpone sólo `tests/test_r8.py`. Las tres fallan, sin errores.
Para N-r7b se conserva el runtime exacto de r5 `5e9dd46` y se superpone
sólo `tests/test_r6.py`, primero el de r7 y después el corregido.

| Hallazgo | Commit de corrección | Prueba y fallo antes frente al pase después |
| --- | --- | --- |
| W-r7a | `4dac297` | `test_closed_high_lane_reconciles_then_h2_enters_first_attempt`: antes H3 uncertain, H3b lane-closed y H2 denied/quota-full. Después exige H3 closed/reconciled, H3b lane-closed y H2 admitted al primer intento. `test_closed_lane_reconciles_only_requested_project_and_lane` exige que H4 y J3 sigan inciertas al cerrar H3. Antes las tres quedan inciertas. |
| N-r7a | `0a7e951` | `test_contract_defines_jale_lane_closed_reselection`: antes falta la conducta de Jale ante lane-closed. Después exige soltar slot, releer estado, reelegir según modo y esperar como ante resource-busy si no queda ninguno libre. Sólo cambia el contrato de U2. |
| N-r7b | `e66c5c6` | Los tres casos reales de sync de `test_r6` contra r5 fallaban con preparación de 0,10 a 0,19 s, incluido el ACK a tiempo. El fixture corregido reconoce las firmas seconds y deadline, observa un plazo absoluto coherente y pasa el argumento original a wait_sync. La aceptación exige control puntual OK, dos fallos reales de r5 y cero errores de fixture. |

No se rebate ningún hallazgo aceptado. Model the Domain mantuvo la
separación existente entre estado de la claim y decisión de admisión.
Reconciliar una claim cerrable no convierte lane-closed en permiso para
usar ese carril. Se reutiliza la prueba de vacío existente bajo el mutex,
sólo para el proyecto y carril solicitados. No cambia la firma del runtime.

## Corridas y gates

Todas las pruebas se ejecutaron exclusivamente en el desk mediante
`REMOTE_RUN_HOST=desk remote-run`, nombre fijo `desk-politica-u1-r8`,
una corrida propia a la vez y `taskset -c 0-1`.
El gate previo y el interno siguen Estado #60. Exigen carga <12,
120 s desde el último arranque en remote-run o slots Jale, MemAvailable
al menos 6 GiB, C: libre al menos 60 GB y raíz WSL libre al menos 100 GB.
Los sondeos y consultas SSH llevan ServerAliveInterval=5. Los fixtures viven
dentro del mirror. La MacBook sólo edita y prepara snapshots y hashes.
No se escribe en `/tmp/sen-test-slots` ni en estado comprador real.

| RunId | rc | Resultado |
| --- | --- | --- |
| `20261105-162939-46640a` | 75 | Gate interno rechaza espaciado de 0,87 s por un arranque concurrente de Jale posterior al gate previo de 148,79 s. No ejecuta pruebas. |
| `20261105-163202-e6865f` | 1 esperado | Tres regresiones r8, tres fallos y cero errores en 9,716 s. Fixture r7 contra r5, tres fallos y cero errores en 54,052 s por interpretar segundos relativos como plazo absoluto. Limpieza sin procesos propios. Gate 6,0801 de carga y 142,04 s. |
| `20261105-163642-96e74e` | 0 | Primera aceptación, 114 pruebas OK en 445,961 s. Control r5 puntual OK y dos defectos reales detectados sin error de fixture. Dos pruebas con demora controlada OK en 35,982 s. Check y limpieza OK. Gate 8,4521 de carga y 135,85 s. |
| `20261105-170029-68caa8` | 0 | Segunda aceptación, 114 pruebas OK en 425,916 s. Control r5 y sus dos negativos verificados. Dos pruebas con demora controlada OK en 31,305 s. Check y limpieza OK. Gate 6,6973 de carga y 131,37 s. |

El gate previo rechazó varios intentos de lectura por espaciado entre
arranques compartidos, sin crear corridas nuevas. `r8-shared-desk.log`
conserva los arranques Jale visibles durante la primera suite y una
muestra durante la segunda con procesos bun de otras worktrees.
Las dos corridas imprimen Python 3.12.3, afinidad [0, 1] y el mismo hash
del módulo. Los manifiestos de entrada pasan en ambas. ShellCheck no
está instalado; se omite sólo su paso condicional.

Salida de W-r7a en las dos aceptaciones:

```text
R8 STALE H3 closed H3b lane-closed H2 first attempt admitted None
R8 SCOPE {'H3': 'closed', 'H4': 'uncertain', 'J3': 'uncertain'}
```

Las regresiones W-r6a de amanecer y writer caído pasan en ambas con ocho
lane-closed por escenario. La r5 de alcance limitado también pasa en ambas,
con `R5 no-writer recovery admitted closed uncertain`.
El fixture sólo normaliza el tiempo observado. No sustituye reloj, select,
lectura del pipe ni el argumento que recibe el wait_sync real. Conserva las
aserciones y los reintentos acotados de r7. Contra r5, el control puntual
pasa y sólo fallan `0 != 75` y `EOF waited until the sync deadline`.
Esos dos fallos del runtime antiguo son resultados exigidos por la sonda,
que termina con rc 0 únicamente si los detecta sin errores de fixture.

## Reproducción e integridad

Los logs completos quedan en `evidence/<runId>.log`. Sólo se retiran
espacios finales. `r8-before.sh` conserva el comando rojo y `r8-full.sh`
el de aceptación. Ambos ejecutan `r8-gate.py` en destino. Antes de cada
start se ejecuta el mismo gate por SSH sin argumento para incluir todos
los arranques. En destino recibe el directorio propio de la corrida para
excluir sólo su arranque.

Para preparar las copias privadas se extrae `git archive 8fc1f91` en
`.audit/r8-base`, y `git archive 5e9dd46` en `.audit/r8-r5` y
`.audit/r8-r5-fixed`. Se copia el test_r8 nuevo en la primera, el test_r6
de `8fc1f91` en la segunda y el test_r6 corregido en la tercera. No se
modifica ningún otro archivo de esas copias. El baseline r7 imprime
`cfa7e47ab282c665f464545a78ad31bbd3ce33e18c2603e3b2355a15c69347bc`.
`r8-r5-fixture.py` exige el hash r5
`77a369608532867e785aa7eff8f496e9a2480ff7a3b8d3b98a4d826901e7200d`
y verifica control positivo y defectos separados.

SHA-256 final de los siete archivos de `store.SOURCE_FILES`:

```text
d8084346baa0275af09f6e353b02671240dbba7b65eab1a87e616f9873e2edd9
```

`r8-inputs.sha256` fija runtime, contrato, pruebas y comandos de aceptación.
Los cambios posteriores de DELIVERY y logs no alteran esos bytes.
No quedan preguntas de producto abiertas ni desviaciones de las decisiones
de PAO. La revisión independiente y el APTO final siguen a cargo de PAO.
Archivos de producto modificados desde r7: contrato y engine.py.
Las pruebas cambian sólo test_r6.py y el nuevo test_r8.py. Se añaden los
comandos, hashes y logs r8 en evidence, y esta entrega.

---

# Entrega U1, corrección para ronda 7

Rama `feat/desk-politica-v1`, preparada con fetch y seguimiento de origin.
El head inicial coincidió con `9567e18f3573ced8acea0aa6eec86c17ff5b627e`.
Se leyeron completos U1, r7, las decisiones anteriores, Sol A, el veredicto
y los dos informes r6. Nivel alto por contrato entre servicios. Trabajo
acotado de implementación: sin descendientes, PR, despliegue ni cambios en
los consumidores. La revisión independiente posterior corresponde a PAO.

## Hallazgos, commits y regresiones

Las ocho regresiones de `tests/test_r7.py` se registraron en `3ff15d3`.
El baseline usa `git archive 9567e18`, superponiendo sólo esas pruebas y los
programas de evidencia nuevos; conserva íntegros el runtime y `test_r6.py`
antiguos. Su SHA-256 es `912e872a4a49c287b77f1753f010eafac4b941e6ef3727a45ee19ddd34c08cfd`.
El primer baseline da ocho fallos esperados, cero errores. El segundo da
dos fallos temporales y cuatro de protocolo de cliente, todos sin errores.
Los rc 1 se conservan como evidencia roja, sin convertirlos en aceptación.

| Hallazgo | Commit | Prueba y diferencia antes → después |
| --- | --- | --- |
| W-r6a opus, carril fuera del modo | `4b1afa3`; protocolo real local en `6e2110e` | `test_dawn_high_lane_is_closed`, `test_dead_writer_high_lane_is_closed`: ocho intentos por escenario devolvían `resource-busy`; ahora exigen `lane-closed`. `test_closed_lane_precedes_busy_claim_and_fifo` comprueba el motivo aun con claim viva y otros pendientes, sin liberar esa claim. Los cuatro casos de `r7-hasito-protocol.py` usan el `estado.py` real: auto reselecciona carril 2 porque el 1 tiene un turno previo, admite de día con ids nuevos; explícito termina con «carril 3 cerrado en el modo vigente»; ambos retiran el turno alto, igual que su sucesor. Antes, los cuatro fallan al recibir `resource-busy`. |
| N-r6a opus, demanda imposible | `403e49f` | `test_closed_lanes_do_not_inflate_night_demand_or_take_jale_quota`: antes añadir carriles 5..8 cambiaba demanda Hasito 2→6 y cuota Jale 2→1; después conserva demanda 2 y cuota Jale 2. `test_day_and_expired_policy_views_ignore_closed_pending_lanes` comprueba publicación diurna y fallback, sin borrar solicitudes. |
| N-r6b opus, byte inválido | `b544ca7` | `test_invalid_sync_byte_closes_without_execution_or_capacity`: antes rc 75/census-unknown, claim y recibo active/null; después rc 75, ambos closed/sync-timeout, salida null, marcador ausente, peso 0 y flock libre. |
| N-r6c opus y nit de sol, tiempos compartidos | `8f7dc9a` | `r7-fixture-schedules.py` añade 5 s sólo a la primera publicación del recibo. Sobre los tests antiguos reproduce `EOF retained capacity until the sync deadline` y `control never reached production select`, dos fallos en 65,765 s. Los tests corregidos observan las operaciones reales y conservan la exigencia de EOF previo al plazo y ACK tardío sin ejecución; la preparación puede reintentarse con ids nuevos y plazos medidos. |
| N-r6d opus, diagnóstico repetido | `c6cbf6b` | `test_closed_claim_diagnostic_once_per_process_claim_and_error`: antes seis mensajes para dos errores, incluidos los de otra instancia del motor; después exige exactamente dos y verifica que se sigue reparando el recibo hasta recuperar salida 0. La deduplicación es por proceso, raíz, boot, request/run y error, sin dejar de reintentar el recibo. |

No se rebate ningún hallazgo. Se implementan las cinco decisiones de PAO.
El módulo devuelve `lane-closed` antes de comprobar ocupación o FIFO para
una pendiente válida fuera del modo. Los replays de claims admitidas y las
claims altas vivas conservan sus garantías anteriores. El writer y `estado`
filtran demanda según el modo efectivo; no usan una cuota como índice.

## Pruebas temporales modificadas

`test_eof_closes_immediately_with_long_deadline` ya no mide desde el fork
con `pipe.poll(4)`. Un observador registra entrada y salida del `wait_sync`
real, el plazo absoluto y la primera entrada en el `select` real. Exige que
EOF devuelva false antes del plazo, además del cierre, peso cero y flock libre.
Si la publicación inicial consumió el plazo, termina esa claim y reintenta,
como máximo tres veces, con plazo derivado de la preparación medida.

`test_late_ack_after_sigstop_never_executes` y su control positivo observan
la entrada real en select, en vez de sondear `/proc/<pid>/wchan`. Si la
publicación agotó el margen, reintentan hasta tres veces con ids nuevos y
`max(plazo_anterior, 2*preparación_medida+1)`. El caso tardío acredita SIGSTOP,
espera al plazo absoluto más 0,3 s, envía el ACK y luego SIGCONT. Conserva
rc 75, salida null, una ejecución para el control y cero para el tardío,
así como replay sin reejecución. No sustituye reloj, select ni lectura del pipe.

El helper `RoundSixTest.run_cli/result` también permite tres esperas
acotadas de 60 s para preparación/publicación, por el tercer timeout de sol
en `test_both_final_publications_fail_then_reconcile_closed_claim`. Conserva
las aserciones de salida, cierre, recibo, identidad y recuperación. Estos
cambios no modifican ningún plazo ni comportamiento del runtime.

## Corridas y gates

Todas las pruebas se ejecutaron exclusivamente en el desk con
`REMOTE_RUN_HOST=desk remote-run`, nombre fijo `desk-politica-u1-r7`, una
corrida propia a la vez y `taskset -c 0-1`. El gate previo y el interno
exigen 120 s desde el último arranque, carga <12 hasta 22:00Z y <9 después.
El primer gate rechazó carga 24,71 sin lanzar nada; otros rechazaron el
espaciado. Antes de la segunda aceptación se esperó unos ocho minutos
por carga de 21–25; el arranque se hizo sólo cuando bajó de 12. Los fixtures y locks de Hasito viven sólo dentro del mirror.
No se escribió en `/tmp/sen-test-slots` ni en estado comprador real.

| RunId | rc | Resultado |
| --- | --- | --- |
| `20261105-144900-f59cac` | 1 esperado | Runtime exacto inicial; ocho pruebas, ocho fallos y cero errores en 22,788 s. Gate 9,9121 / 263,77 s. |
| `20261105-145553-216226` | 1 esperado | Dos fallos temporales en 65,765 s y cuatro de cliente en 34,218 s, cero errores. Gate 3,0259 / 136,92 s. |
| 20261105-145913-ebe578 | 0 | Primera aceptación compartida: dos temporales con demora OK en 32,139 s; 111 pruebas OK en 717,670 s; cuatro casos Hasito OK en 42,538 s; check y limpieza OK. Gate 3,0864 / 122,82 s. |
| 20261105-152034-30dc6a | 0 | Segunda aceptación compartida: dos temporales con demora OK en 31,556 s; 111 pruebas OK en 471,640 s; cuatro casos Hasito OK en 42,847 s; check y limpieza OK. Gate 10,0083 / 546,80 s. |

Los logs completos están en `evidence/<runId>.log`; sólo se eliminan espacios
finales de línea. `r7-before.sh` y `r7-before-extra.sh` conservan los comandos
rojos. Para prepararlos se extrae el head inicial en `.audit/r7-baseline`, se
superpone el `test_r7.py` nuevo y se copian `r7-fixture-schedules.py` y
`r7-hasito-protocol.py` a su directorio de evidencia. El segundo exige el
hash exacto del runtime inicial; el primero lo imprime para contrastarlo.
El `estado.py` verificado va como `hasito-estado.py` en ese baseline y como
`.audit/r7-hasito-estado.py` para la aceptación.

`r7-full.sh` verifica `r7-inputs.sha256`, ejecuta las dos pruebas con demora
controlada, la suite completa, los cuatro casos de FIFO local real,
`bin/pao check`, limpieza y ShellCheck si está instalado. ShellCheck no
está instalado en el desk y su paso condicional se omitió. El primer gate de aceptación registró la corrida ajena `20261105-144401-065670`, con
Bash, Python y Docker trabajando en `sen4198-r3-final-krkmbHVf`. El gate registra
árboles de corridas ajenas para acreditar el desk compartido, descartando
PIDs reutilizados. `r7-cleanup.py` comprueba que no quedan procesos propios
no zombies fuera de los controles de la corrida.

## Integridad y límites

SHA-256 final de los siete archivos del runtime, con el algoritmo de
`store.code_digest`:

```text
cfa7e47ab282c665f464545a78ad31bbd3ce33e18c2603e3b2355a15c69347bc
```

La MacBook sólo editó, preparó snapshots/hashes y leyó evidencia.
El código y las pruebas de aceptación quedan fijados por
`evidence/r7-inputs.sha256`; las actualizaciones posteriores de entrega y
logs no los cambian. El protocolo Hasito se ejercita mediante un cliente de
prueba y su `estado.py` real, SHA-256
`2efcf5fc350965ec5bb0fee438e941d912d07b15679a2f7dca911090a17db006`.
Eso valida el contrato de U1; el adaptador real de `run.py`/remote-run se
implementa y verifica en U3. No se afirma una integración desplegada.
Archivos modificados: `fleet/contracts/desk-politica-v1.md`,
`fleet/deskpolicy/{engine.py,cli.py,README.md,DELIVERY.md}`,
`tests/{test_r6.py,test_r7.py}` y los comandos, hashes y logs r7 en `evidence/`.
`model.py`, `store.py`, `testing.py`, `installation.py`, instalación y retirada
conservan sus bytes iniciales.
No hay desviaciones ni preguntas de producto abiertas en esta corrección.
PAO conserva la review independiente, el APTO y la activación.

---

# Entrega U1, corrección para ronda 6

Felix quiere más corridas nocturnas en el desk con reservas conservadoras y
una sola autoridad de orden local para Hasito. Tras `git fetch origin
feat/desk-politica-v1`, el remoto y la rama local coincidieron con
`5e9dd46b5ea62f94646dc0314362456dcb17067c`. Se cambió a esa rama siguiendo
origin en esta worktree con `--ignore-other-worktrees`, sin editar las demás.
Se leyeron completos el brief U1, las correcciones r2 a r6, Sol A, el veredicto
y ambos informes r5, además de los repros citados.

Nivel alto por contrato entre servicios. Se aplicó Bug fix de felix-mode como
trabajador, sin descendientes, PR ni despliegue. Attack the Premise cambió el
orden de Hasito: la premisa de que dos FIFO independientes conservan el orden
queda refutada por el baseline `resource-busy / denied fifo`. Model the Domain
se concretó en un `lane` de solicitud inmutable y una fusión monótona común
para los recibos, sin añadir otra autoridad ni otro archivo de estado.
La review independiente y el APTO siguen a cargo de PAO.

## Cambios y regresiones

Las regresiones se guardaron antes del runtime, desde `9c07e87`.
`tests/test_r6.py` añade 14 casos. El baseline corregido de
`20261105-130807-d8d7ab` usa `git archive 5e9dd46`, con sólo ese archivo
superpuesto, y registra **14 pruebas, 13 fallos esperados, cero errores**.
El control de ACK a tiempo pasa ya antes. Los cambios de fixtures `c0003d9`,
`d13b69f` y `51c5d19` se distinguen de las correcciones del runtime.

| Hallazgo / decisión | Commit | Prueba, fallo antes y resultado corregido |
| --- | --- | --- |
| W-r5a sol, ACK tardío | `479c754` | `test_late_ack_after_sigstop_never_executes`, más `r6-original-sync-late.py`, copia literal del revisor. Antes: ACK tras 3,3 s ejecuta, rc 0, cierre completed. Después: rc 75, closed/sync-timeout, salida null, marcador ausente, peso 0 y flock libre. `test_timely_ack_executes_exactly_once` comprueba una ejecución y replay sin otra. |
| W-r5b sol, recibo conocido | `5fe6637` | `test_original_receipt_failure_reconcile_and_replay_recover_exit`, `test_both_final_publications_fail_then_reconcile_closed_claim` y `test_closed_receipt_unknown_fields_fill_without_replacing_known_values`, más `r6-original-receipt-recovery.py` literal. Antes: recibo closed/reconciled con salida null aunque claim y replay dicen 3; las claims ya cerradas no se recuperan en un tick. Después: salida 3, sin reejecución. También se preservan identidad, un código conocido 7 y causa stopped frente a datos durables distintos. |
| W-r5a opus, orden de Hasito | `31fb668` | `test_local_first_sync_timeout_owner_advances` y `test_local_first_suspension_owner_advances` fallan antes por la ausencia de la obligación contractual local-first; su control funcional con el protocolo nuevo ya avanza en r5. `test_busy_lane_head_does_not_block_free_hasito_lane` falla funcionalmente antes con denied/fifo y después admite el carril 2 mientras A recibe resource-busy en el 1. `test_pending_lane_is_immutable_and_admission_matches` impide cambiar el N registrado. El script `r6-hasito-protocol.py` ejecuta los tres escenarios con la FIFO real de Hasito. |
| Decisión 4, orden del keeper | `4dd10b5` | `test_original_c2_load_before_registration_rejected` y `test_original_cr3_load_before_registration_rejected` conservan la carga real close_fds de los escenarios C2/C-r3 y añaden la llamada de traspaso exigida ahora. Antes: accepted. Después: handoff-load-before-registration; launcher conserva flock, servidor sigue vivo, reconciliación no cierra y reemplazo recibe resource-busy. Los cuatro `test_c2_*` conformes permanecen en la suite. |
| N-r5a opus, EOF | `479c754` | `test_eof_closes_immediately_with_long_deadline`: antes sigue reteniendo capacidad a los 4 s de un plazo de 8 s; después cierra inmediatamente con sync-timeout, salida null, peso 0 y flock libre. |
| N-r5c opus, máximo 900 | `479c754` | `test_cli_rejects_sync_timeout_over_900_before_claim`: antes admite y ejecuta con 900,1; después devuelve 75 sin claim. El control de r5 conserva la omisión de 900. |
| N-r5a sol / N-r5b opus, nombres y renovación; N-r5d opus, slot incierto | `31fb668` | `test_contract_names_turn_files_and_no_active_turn_renewal`: antes faltan turnos.json, turnos.lock, la regla de turno activo y la excepción de admitir; después están los tres archivos con su función, no hay renovación activa ni subcomando renovar y se añade «salvo que admitir la reconcilie». |

La solicitud Hasito declara `pendiente --lane N` después de obtener el lock
local N. Sólo su poseedor renueva espera remota; los sucesores sólo están en
la FIFO local. Conserva lock y turno activo al reemitir con nuevos request y
run id por vencimiento o sync-timeout. La cabeza PAO es por carril. Para
mostrar más demanda nocturna se pueden usar hasta `estado.max_lane` locks,
incluidos desk-3.lock y desk-4.lock. Los registros sin `lane` se mantienen
legibles para Jale y clientes de bajo nivel; las pendientes antiguas de Hasito
se drenan en el corte y, mientras existan, bloquean conservadoramente por seq.
La CLI de Hasito exige el carril. El fixture histórico de peso excesivo ahora
lo declara; no se altera su expectativa de `weight-too-large`.

La reconciliación recupera también los recibos de claims ya cerradas y sólo
escribe si cambió algún campo desconocido. El replay comparte la misma fusión.
`claim_state` sólo avanza; `script_exit_code`, `close_cause` y el diagnóstico
conocido no se borran ni reemplazan. El recibo conserva su identidad admitida.
No se usa el TTL para liberar una claim ni se toca el algoritmo de presupuestos.

## Corridas en el desk

Todas las pruebas se ejecutaron con `REMOTE_RUN_HOST=desk remote-run`, una
corrida propia a la vez, nombre fijo `desk-politica-u1-r6` y `taskset -c 0-1`.
El gate previo y el interno exigen carga < 12 hasta 2026-11-05T22:00:00Z y
< 9 después, además de 120 s desde el último arranque ajeno en remote-run.
Los fixtures y locks Hasito de prueba viven dentro de sus mirrors privados.
La MacBook sólo preparó archivos y leyó evidencia. No se escribió en
`/tmp/sen-test-slots` ni en `~/.local/state/comprador` de ningún host.
No se ejecutaron despliegue ni retirada. Hostgate no cambia.
La primera aceptación arranca con carga 6,6294 y 135,24 s de espaciado;
registra otras dos corridas activas. La segunda arranca con carga 2,8701 y
413,62 s de espaciado. No se detuvo ni modificó ninguna corrida ajena.
La comprobación focalizada
arranca con carga 1,9292 y 124,58 s de espaciado. Los censos finales de
`/proc` no encuentran procesos no zombies con cwd en los mirrors propios;
se conservan en `r6-cleanup-first.log` y `r6-cleanup-final.log`.

| RunId | rc | Resultado |
| --- | --- | --- |
| `20261105-130148-253b38` | 1 | Baseline inicial. Runtime exacto de 5e9dd46, hash 77a36960…200d. Los dos repros literales fallan como en r5. Los 13 casos propios dan 12 fallos; uno era un contador del fixture en memoria privada del fork y no acredita ese caso. |
| `20261105-130429-761556` | 1 | Primera tanda focalizada corregida. Los dos repros literales pasan; 12 de 13 pruebas propias pasan. La restante exige tres publicaciones cuando el repro original sólo hace dos en total; se corrige el fixture y se añade el escenario separado de dos publicaciones finales fallidas. |
| `20261105-130807-d8d7ab` | 0 | Baseline corregido con 14 pruebas, 13 fallos esperados y cero errores. Dos adversarios OK en 9,039 s y 103 pruebas OK en 389,102 s. Repros literales, tres casos Hasito reales, C3 y bin/pao check pasan. |
| `20261105-131640-38ceae` | 0 | Segunda aceptación. Dos adversarios OK en 8,980 s y 103 pruebas OK en 409,955 s. Repros literales, tres casos Hasito reales, C3 y bin/pao check pasan. Su intento residual de baseline no se cuenta, como se explica abajo. |
| `20261105-132503-6999ec` | 0 | Comprobación focalizada final del wrapper y la CLI. Cache-only se omite; importaciones fallidas del runtime o de las pruebas dan baseline rc 2 y rechazo. Registro y renovación de lane 2 pasan; cambiar a lane 1 devuelve 75 sin claim. |

Los logs completos se guardan en `evidence/<runId>.log`, normalizando sólo
espacios al final de línea. El baseline corregido está al principio del log
de la primera aceptación; su rc 1 es esperado y comprobado por el wrapper.
`r6-regressions.sh` ejecuta las regresiones focalizadas y ambos repros literales.
`r6-harness-check.sh` valida por separado el wrapper final y la CLI pública
de registro por carril, sin repetir la suite principal. Comprueba que una
carpeta con sólo caché se omite y que errores de importación del runtime o
de las pruebas se rechazan con rc 2 del baseline, en vez de aceptarlos como
regresiones. También registra y renueva lane 2 y rechaza su cambio a lane 1,
con cero claims nuevas.
`r6-full.sh` ejecuta los adversarios W11/W12 de r5, la suite completa, los
repros literales, C3 original y las tres sondas Hasito. Termina con
`bin/pao check` y ShellCheck si está instalado. Un baseline opcional en
`.audit/r6-baseline` permite repetir el fallo antes en la misma corrida,
siempre con cwd y afinidad dentro del mirror. El wrapper final exige el hash
exacto de 5e9dd46, 13 fallos y cero errores; un fallo de importación sale 2.
En la segunda aceptación rsync dejó un directorio residual de __pycache__
sin fuentes del baseline. El wrapper anterior lo detectaba sólo por ser
directorio e intentó importar desde él. Ese ModuleNotFoundError no acredita
un baseline; sólo se cuenta el de la primera aceptación. El guard final
comprueba el archivo de prueba. No cambia runtime ni suite principal.

Para reproducir las sondas Hasito se copia su `scripts/mini/estado.py` a
`.audit/r6-hasito-estado.py`, antes de sincronizar el repo. Se usó la copia
verificada en r5 de Hasito main `002c6ad5ceaa412c4fd125617c27db2c2d2436b0`,
SHA-256 `2efcf5fc350965ec5bb0fee438e941d912d07b15679a2f7dca911090a17db006`.
El script rechaza otra versión; no se modifica ese código ni se usan sus
rutas reales de estado. No es una integración desplegada de U3.

## Integridad y precisiones

SHA-256 final de los siete archivos del módulo, calculado por `code_digest`:

```text
912e872a4a49c287b77f1753f010eafac4b941e6ef3727a45ee19ddd34c08cfd
```

`evidence/r6-inputs.sha256` identifica contrato, README, runtime, pruebas y
programas de evidencia. Incluye el guard final del wrapper y su comprobación
focalizada posterior a las suites. Ambas suites usan los mismos siete archivos
del runtime y los mismos archivos de las 103 pruebas. ShellCheck no está
instalado en el desk; su paso condicional se omite. La comprobación focalizada
valida además la sintaxis del wrapper final con `bash -n`.
Los commits posteriores de evidencia y DELIVERY no
cambian el runtime probado.

No se rebaten hallazgos ni decisiones aceptadas. Sí se precisa el conteo del
repro original del recibo: con un hijo vivo, `cerrar` falla antes de llegar a
su publicación final. Se intenta entonces una sola publicación de recuperación,
además de la inicial. La regresión propia acredita exactamente dos llamadas
a `cli.atomic_json`; el escenario adicional acredita tres llamadas, con las
dos finales fallidas, y recuperación desde una claim ya cerrada. El primero
concluye closed/reconciled/3 y el segundo conserva su causa durable completed.
No se sustituye una causa conocida para hacer coincidir una expectativa.

Archivos cambiados r6: contrato; README y DELIVERY; `cli.py`, `engine.py`;
fixtures `tests/test_policy.py` y `test_r2.py`; nueva `tests/test_r6.py`;
wrappers, repros, sondas, manifiesto y logs `r6` en `evidence/`.
Sin preguntas de producto abiertas. Los gates de U2/U3, revisión y activación
siguen siendo responsabilidad de sus unidades y de PAO.

---

# Entrega U1, corrección para ronda 5

Felix quiere recuperar la capacidad del desk cuando una corrida terminó,
conservarla mientras quede carga y dar a Hasito un protocolo de sync acotado.
Se comprobó `origin/feat/desk-politica-v1` en
`88bee0891d543010ddd79cf87f3a07b4314f6949` después del fetch requerido.
Se cambió a la rama existente en este worktree con
`git switch --ignore-other-worktrees`, sin editar los otros worktrees.
Se leyeron el brief U1, los fixes r2 a r5, ambos informes r4, sus scripts
y logs de evidencia, el candidato Sol A y el veredicto del juez.

Nivel alto por contrato entre servicios. Se siguió Bug fix de felix-mode.
La premisa de r4 de que cualquier PID desaparecido invalida el vacío quedó
refutada por el baseline. Se aplicó la nueva decisión de PAO, tres barridos
sin miembros separados al menos 50 ms, ignorando ENOENT y ESRCH.
Las regresiones entraron primero en `788b376`. No hubo descendientes, PR,
despliegue ni ejecución de los guiones de instalación o retirada. No se
escribió en `/tmp/sen-test-slots` ni en `~/.local/state/comprador`.
La revisión independiente y el APTO operativo siguen a cargo de PAO.

## Cambios y prueba por hallazgo

Las regresiones nuevas están en `tests/test_r5.py`. El baseline usa el runtime
y los fixtures originales de `88bee08`, con sólo ese archivo superpuesto.
`20261105-112754-50d388` registra el fallo antes. El run
`20261105-113029-9da1d4` registra los 14 casos pasando después.

| Hallazgo | Commit | Fallo antes y pase después |
| --- | --- | --- |
| W-r4b, vacío probado | `9090c8e` | ENOENT y ESRCH daban `None`; el tercer barrido no existía (`2 != 3`) y no veía su miembro. Después se ignoran sólo esas desapariciones, se exigen tres barridos separados 50 ms y se detecta al miembro del tercero. EIO, permisos, parseo, cambio de identidad y zombies conservan su resultado conservador. La medición de 20 cierres y C3 se detallan abajo. |
| W-r4b, sin writer | `9090c8e` | `admit_reconciles_only_uncertain_claim_of_requested_resource` imprimió `denied uncertain uncertain`. Después imprime `admitted closed uncertain`: sólo cierra la claim vacía del mismo recurso. La otra claim sigue reservada. |
| W-r4a, verify sin keeper | `9090c8e` | La preparación fallida dejaba la verify incierta después de terminar y recoger al hijo. Después cierra como reconciled, conserva salida 1 y admite otra verify. El keeper vivo sin registrar impide cerrar por su flock; al salir sin haber lanzado hijos, sí puede cerrar. |
| W-r4c, FIFO de Hasito | `2821fb0` | Faltaban `MINI_LOCK`, FIFO por lock y la regla de no cambiar carril. El control documental falla antes y pasa después. Cada nombre fija un carril, el turno conserva su cabeza y renueva ambos registros. Sólo la elección sin lock explícito busca el menor lock libre sin adelantar a filas existentes. |
| W-r4d, sync | `515a748` | El baseline no reconoce `--sync-ready-fd` ni tiene el plazo en la CLI. Después `--sync-timeout-s` vale 900 por omisión. Con un plazo real de 0,15 s, sin señal, devuelve 75, cierra con `sync-timeout`, deja salida null, libera peso y flock y no crea el marcador del guion. Un replay devuelve 0 sin ejecutarlo. Con señal `1` ejecuta una vez y cierra normalmente. |
| N-r4a, N1, EMFILE | `33eaa5d`, `dcb6e50` | Con el límite privado de fds, el baseline reproduce `OSError: [Errno 24] Too many open files` en `worker.start()`. Después 30 llamadas a la CLI pasan dentro del mismo límite y quedan como máximo cuatro fds adicionales. Los controles positivos repiten una prueba real hasta 60 s, sin sustituir inconcluso por vacío. |
| N2, adversario W11 | `33eaa5d`, `37f2162` | El informe r4 sol registra rc 1, W11 con `75 != 0`, sobre `88bee08`. Ahora el replay espera por evento a que termine el primer writer, fuera del mutex. El adversario inyecta un primer cierre inconcluso y comprueba su reintento, el mutex de cada escritura y el recibo terminal. W12 conserva la contención real de 5 s y su salida 3. Ambos pasan en las dos aceptaciones completas. |
| N-r4b, recibo en el tick | `9090c8e` | JSON inválido en el recibo propagaba `Invalid` desde `publish()`. Después la claim queda cerrada, también cierra la otra claim y publica revisión 1. Registra `pao/desk-reconcile-diagnostic@1` por stderr con identidad y error, sin `exit_code`. |
| N-r4c, estado | `9090c8e` | Antes muestra `('day', 4, None)`. Después muestra `budget=null`, `max_lane=2` y cuotas 2/2 de día. De noche muestra presupuesto 4 y `max_lane=4`. |

El fallo anterior de N2 procede de la revisión sol del head `88bee08`, run
`20261105-105440-fee4aa`. `evidence/r5-w11-before-review.log` conserva su log
completo, copiado de `reviews/r4-sol-progress.log`. No se presenta como una
corrida nueva de este trabajador.

EMFILE pertenece al fixture, no a los archivos del módulo. `run_cli` retenía
la conexión y el handle de cada `multiprocessing.Process` hasta tearDown,
incluidos sus descriptores de notificación. El bucle de reconciliación de r3
podía acumularlos durante 60 s. Ahora los recoge y cierra al consumir el
resultado, y tearDown también cierra los handles restantes. El control de
50 ciclos de registro, estado y reconciliación no aumenta los fds del módulo,
tanto en el baseline como en el arreglo.

## C3, traspasos y cambio explícito de contrato

`evidence/r5-original-c3.py` conserva sin cambios el primer escenario de
`reviews/sol-r3-snapshot/review-new.py` hasta su bloque de keeper. Sólo añade
aserciones de sus resultados. El hijo nuevo no estaba en el listado original,
pero sigue vivo después del fork. El resultado corregido es `closed=[]`,
claim incierta, reemplazo denegado y `R5 ORIGINAL C3 PASS`.
La suite también ejecuta C3 durante `cerrar`, donde conserva salida 3,
además del caso de reconciliación con el padre ya recogido.

Se mantienen las regresiones de r4 para keeper registrado muerto antes del
primer cierre, segundo traspaso, servidor vivo en una sesión anterior y
cierre positivo tras vaciar todas las sesiones. El caso antiguo
`c2_missing_keeper_registration_never_reconciles` lanzaba un servidor desde
un keeper sin registrar y luego mataba al keeper. Ese escenario viola ahora
la obligación explícita de registrar el SID antes de lanzar cualquier hijo.
Se sustituyó por el keeper vivo sin registrar que conserva el flock y por
la preparación fallida sin traspaso. No se afirma proteger carga que viole
ese orden. Tampoco se afirma eliminar la cadena de fork y salida que esquive
tres barridos; ese riesgo aceptado está en el contrato.

## Corridas y reproducción

Todas las pruebas corren en el desk mediante `REMOTE_RUN_HOST=desk remote-run`,
una propia a la vez, con el nombre fijo `desk-politica-u1-r5`. Los wrappers
comprueban el mirror, carga y 120 s desde el último `started`, con fallback al
mtime del directorio. Aplican carga menor de 12 hasta 2026-11-05T22:00:00Z y
menor de 9 después. Ejecutan pruebas y generador bajo `taskset -c 0-1`.
Los fixtures quedan dentro del mirror. La MacBook sólo prepara archivos.

| RunId | rc | Resultado |
| --- | --- | --- |
| `20261105-112219-74d822` | 1 | Primer baseline, 14 casos en 138,747 s, ocho fallos y nueve errores. Dos mocks tenían una firma incorrecta; este run no acredita esas sondas. Se conserva completo y se sustituye por el siguiente. |
| `20261105-112754-50d388` | 1 | Baseline corregido sobre `88bee08`. 14 casos en 136,224 s, nueve fallos y cuatro errores esperados: EMFILE, recibo inválido y dos por CLI de sync ausente. E/S inconclusa y ausencia de fuga del módulo pasan ya en este head. |
| `20261105-113029-9da1d4` | 0 | Los 14 casos r5 pasan en 30,363 s. |
| `20261105-113316-e830d6` | 0 | Primera aceptación completa, dos adversarios en 7,988 s y 89 pruebas en 505,921 s. C3 original pasa, 20 cierres y 40 admisiones sin writer pasan, y `bin/pao check` pasa. |
| `20261105-115531-e80c57` | 1 | Churn sobre el runtime exacto de `88bee08`. De 20 cierres, 14 cierran directamente y seis tras un tick. Sin writer, 12 corridas cierran, dos quedan inciertas y las otras 26 reciben `resource-busy`. Falla la aserción de recuperación. Generador de 100 forks/s. |
| `20261105-115822-07304d` | 0 | Segunda aceptación de los mismos 22 archivos. Dos adversarios en 8,887 s y 89 pruebas en 340,300 s. C3 original pasa, los 20 cierres son directos, las 40 admisiones sin writer cierran y `bin/pao check` pasa. |

Las dos suites completas pasan. Incluyen sintaxis Python 3.9 y shell.
ShellCheck no está instalado, por lo que su paso condicional se omite.
La comprobación final de `/proc` devuelve `LIVE_OWN_MIRROR_PROCESSES []` en
ambos mirrors propios. Se conserva en `evidence/r5-cleanup.log`.

Los logs completos están en `evidence/<runId>.log`. Sólo se quitan espacios
al final de las líneas. Los wrappers reproducibles son
`evidence/r5-regressions.sh`, `evidence/r5-full.sh` y
`evidence/r5-churn.sh`. El baseline se genera con `git archive 88bee08` y no
se modifica su runtime. El hash que imprime es
`caac735af226b3bca4eff0cf8d297313b52e153ea4f00e58b6bc66745abf14d0`.

La primera suite coincide con las corridas ajenas `20261105-113037-3ccfb3`
y `20261105-090720-94f8cc`. Además del censo inicial, la muestra de
`evidence/r5-concurrent-desk.json` acredita un descendiente de la segunda
con 101 ticks de CPU en un segundo, a 100 ticks/s. No se detuvo ni alteró
ninguna corrida ajena. La carga inicial fue 2,4287 y el espaciado 136,16 s.
Después de terminar la aceptación se rechazaron nuevos arranques por cargas
15,26 y 18,53. No se relajó el límite.
El baseline de churn arrancó con carga 10,5688 y espaciado 1.335,61 s.
La segunda aceptación arrancó con carga 2,7295 y espaciado 170,52 s.

## Medición de recuperación

`evidence/r5-churn.py` usa un generador en otra sesión, con afinidad `[0, 1]`,
que crea y recoge procesos a una tasa objetivo de 100 forks/s. La tasa se
calcula con sus propios forks y tiempo monótono, sin contar otros procesos
del desk. Se comprueba su rc, su afinidad y una tasa medida de 80 a 110/s.
Los 20 cierres ejecutan `/bin/true` y verifican el estado durable; sólo cuentan
0 ticks si ya está cerrado y 1 si cierra tras una reconciliación adicional.
Si sigue abierta, registran null y fallan. No se convierten errores en cierres.

En la primera aceptación el generador produjo 6.663 forks en 66,6301 s,
99,9999/s. Los 20 cierres quedaron `closed` al primer intento, peor caso
**0 ticks adicionales**. En el mismo generador, las 40 corridas de día sin
writer entraron y cerraron. El censo final fue Jale 0, Hasito 0.
Es una comprobación de recuperación bajo esta carga, no una garantía universal
de latencia ni una medición de velocidad del desk.

La segunda aceptación repite el resultado, con 4.701 forks en 47,0108 s,
99,9982/s. Sus 20 cierres también son directos, peor caso **0 ticks
adicionales**, y las 40 corridas sin writer cierran. No hubo rechazos ni
salidas distintas de 0 en esos dos bloques de ninguna aceptación.

El baseline de la misma medición produjo 8.735 forks en 87,3501 s,
99,9999/s. Sus 20 cierres también cumplieron la cota de un tick en esta muestra,
pero seis requirieron ese tick frente a cero en el arreglo. No se afirma que
ese bloque haya fallado la cota. El fallo está en las 40 corridas sin writer,
con dos claims retenidas y 26 rechazos. Ambos carriles quedan ocupados al
terminar ese bloque. Las regresiones directas del tercer barrido y de los
PIDs desaparecidos sí fallan contra `88bee08`.

## Identidad del artefacto

El código probado está en `dcb6e50`. Los commits siguientes documentan evidencia
y quitan una línea vacía al final de `tests/test_r5.py`.
SHA-256 de los siete archivos de `store.SOURCE_FILES`:
`77a369608532867e785aa7eff8f496e9a2480ff7a3b8d3b98a4d826901e7200d`.
`evidence/r5-inputs.sha256` fija los 22 archivos de runtime, pruebas, contrato,
README y scripts de aceptación. Su hash es
`646578e5eaf827a99f7fe78f849cd87f1e067bce68e34efb8636e9915abc35a0`.
Las dos copias remotas verifican los 22 hashes sin diferencias. Los resultados
están en `evidence/r5-inputs-first.log` y `evidence/r5-inputs-second.log`.
La suite imprime Python 3.12.3 y afinidad `[0, 1]`.

El ajuste final de formato no cambia el AST del test ni ningún archivo del
runtime. La comparación está en `evidence/r5-format-only.log`. Se conserva
el manifiesto original de los archivos que ejecutaron ambas aceptaciones.
`evidence/r5-final-inputs.sha256` describe los archivos finales, con ese único
salto de línea eliminado. Su hash es
`5ad6a2521838c36b028d30cd5ddaf8e2b4cc7d190c70ec8cbb188dc98a3ef9e4`.
`git diff --check 88bee08` pasa tras el ajuste. No se repiten las suites por
un cambio que sólo quita una línea vacía.

Archivos cambiados r5: contrato; `cli.py`, `engine.py`, `store.py`; README y
este DELIVERY; `tests/test_policy.py`, `test_r2.py`, `test_r3.py`, `test_r4.py`
y `test_r5.py`; scripts, manifiesto y logs r5 de `evidence/`.
No se rebaten hallazgos aceptados ni quedan preguntas de producto.
El historial siguiente describe versiones anteriores, sustituidas por r5
donde difieren las decisiones de vacío, verify y FIFO.

---

# Entrega U1, corrección para ronda 4

Felix quiere más corridas nocturnas en el desk sin reutilizar recursos ni CPUs
mientras pueda quedar carga viva. Se comprobó `origin/feat/desk-politica-v1`
en `568ec764d6f1e47c7c43dbff977293ff3cdf7220` tras el fetch requerido.
Se usó esa rama en esta worktree con `git switch --ignore-other-worktrees`,
sin editar las otras worktrees. Se leyeron briefs U1 y r2/r3/r4, candidato
Sol A, veredicto del juez, reviews r1/r2/r3 y los repros citados de r3.

Nivel alto por contrato entre servicios. Se siguió Bug fix de felix-mode como
trabajador, sin descendientes ni PR. Attack the Premise cambió la prueba de
cierre. La premisa anterior, flock libre más sesión original vacía, se probó
falsa con servidores de un keeper y con un fork durante el listado de `/proc`.
Las regresiones quedaron primero en `85834db`, antes de tocar el runtime.
No se desplegó ni se ejecutaron los guiones de instalación o retirada.
No se escribió en `/tmp/sen-test-slots` ni en `~/.local/state/comprador`.
La review independiente y el APTO operativo siguen a cargo de PAO.

## Resultado y contrato

`traspasar` registra bajo el mutex la identidad completa de cada keeper en
`claim.keepers`, antes de ACK al launcher, liberación de su copia del fd de
vida o carga en la sesión nueva. Repetir la identidad es idempotente.
Los traspasos posteriores agregan identidades y nunca reemplazan el historial.
`cerrar` ya no descubre ni registra keepers. Tanto cierre como reconciliación
comprueban todas sus sesiones. Una verify sin registro conserva incertidumbre.

La prueba de sesión distingue `[]`, una lista con miembros y `None` inconcluso.
Exige dos barridos completos vacíos separados 20 ms. Cuenta zombies hasta
reap. Una desaparición, lectura fallida o cambio de identidad hace inconclusa
la prueba. El inicio del barrido también veta una identidad recién nacida,
incluida reutilización de PID antes de su primera lectura. Ambos consumidores
cierran sólo con vacío probado; el cierre incierto conserva la salida conocida.

Hasito elige el menor lock local libre permitido y pasa `--lane N` y
`--desk-lane N` explícitos. Ante `resource-busy` suelta ese lock sin sincronizar
y prueba el siguiente. El contrato divide start en control/admisión, sync y
carga. Exige reserva de mirror, lock local y admisión antes de rsync.
`any` permanece para clientes sin reserva local previa y no es normativo para
Hasito ni Jale. La implementación de esas fases corresponde a U3.

Al reconciliar se actualiza el recibo fijo bajo el mutex con cierre y causa,
sin modificar `script_exit_code` ni la identidad de admisión.
El diagnóstico de cierre usa `pao/desk-close-diagnostic@1` sin `exit_code`.
Con `--recibo`, el wrapper emite una sola línea JSON propia en stdout.
W11 prepara ambos actores antes de disputar el mutex. W12 usa eventos de
arranque, timeout real y liberación; ninguna depende de una pausa elegida.
Los fixtures adoptan y recogen sus huérfanos, sin depender del reap de PID 1.
En `898bd9d`, el caso SIGKILL confirma reap del runner con un evento antes
de llamar cerrar. Los controles positivos de cierre/reconciliación repiten
la operación real si el barrido es inconcluso; nunca sustituyen la prueba de
`/proc` ni transforman una prueba inconclusa en vacío. `3276f67` aplica el
mismo control positivo a W11 y comprueba posesión del mutex en cada escritura,
para mantener la regresión del recibo fuera del mutex. El censo concurrente
espera hasta 60 s a su señal de recibo final, en vez de 10 s.

## Hallazgos, commits y regresiones

Los nombres siguientes omiten `test_`. Las regresiones nuevas están en
`tests/test_r4.py`. `evidence/r4-fixture-schedules.py` fuerza las dos demoras
que daban falsos rojos en W11/W12, con eventos y una barrera antes del exec.

| Hallazgo | Commit | Fallo sobre 568ec76 y resultado corregido |
| --- | --- | --- |
| C2 / C-r3 | `61ff909`, ACK idempotente `66c0d30`; fixtures `a3bdea9` | `c2_keeper_death_before_first_close`, `c2_missing_keeper_registration_never_reconciles`, `c2_second_handoff_tracks_every_session`: antes `closed=['handoff']` con servidor vivo; después `closed=[]`, uncertain y reemplazo `resource-busy`. El segundo caso registrado deja vacía la última sesión y carga sólo en la anterior. `c2_close_checks_previous_keeper_session` antes cierra con esa carga, después devuelve close-unproven y conserva salida 3. `c2_close_after_all_handoff_sessions_drained` conserva el control positivo. |
| C3 | `61ff909` | `c3_fork_during_reconcile_snapshot` y `c3_fork_during_close_snapshot`: antes cierran con el hijo nuevo vivo; después conservan uncertain y salida 3 al cerrar. Se pausa el iterdir real, se hace fork, se recoge al padre y se entrega su lista original, sin inventar entradas de `/proc`. Se prueban por separado desaparición, lectura fallida, identidad cambiada, zombies, segundo barrido con miembros y dos barridos vacíos. |
| W-r3 | `e6131d9` | `wr3_hasito_contract_requires_local_explicit_lane_before_sync`: antes exige any y rsync precede admisión; después exige índice explícito, salto ante busy y las tres reservas antes del sync. La prueba verifica el contrato, no acredita implementación U3. |
| N-a | `61ff909` | `na_reconcile_updates_fixed_receipt_without_replay`: antes `(uncertain, 3, None)`; después `(closed, 3, reconciled)`, conservando execute/replay originales. |
| N-b | `61ff909` | `nb_close_diagnostic_is_separate_and_stdout_single_line`: antes desk-error con exit_code 75 y dos líneas; después desk-close-diagnostic sin exit_code, salida 3 y una línea. Las regresiones W3 con hijo vivo siguen en la suite. |
| N-c | `a3bdea9`, `3276f67` | `r4-fixture-schedules.py`: antes W11 falla en prepare por mutex-timeout y W12 falla por marca ready ausente; después ambos pasan con los mismos eventos de demora. La suite también ejecuta los dos tests de r3 sin ese adversario. |

No se rebaten hallazgos aceptados. No se cambian presupuestos, hostgate ni
adaptadores de consumidores. Los esquemas nuevos siguen pendientes de corte
con drenado y consumidores coordinados; no se migran claims vivas antiguas.

## Ejecuciones en el desk

Todas usan `REMOTE_RUN_HOST=desk remote-run`, una corrida propia a la vez,
`taskset -c 0-1` y fixtures dentro del mirror. Los gates previos y de destino
comprueban 120 s desde `started`, con fallback al mtime del directorio.
Se respeta carga menor de 12 hasta 2026-11-05T22:00:00Z y menor de 9 después,
según Estado #58. La MacBook sólo preparó código, snapshots y hashes.

| RunId | rc | Artefacto y resultado |
| --- | --- | --- |
| `20261105-093405-4e755e` | 1 | Primer fixture sobre 568ec76. 23 tests por importar también los 14 de r3, ocho fallos. Reprodujo C2, C3 y los nits; heredaba descriptores de multiprocessing que demoraban join. Se corrigió el fixture, sin modificar runtime baseline. |
| `20261105-094012-8fd708` | 1 | Baseline exacto, 15 tests aislados, 13 fallos y un error por lectura fallida de stat. El control de cierre limpio pasa. |
| `20261105-094229-937b07` | 0 | Primer runtime corregido, hash a24eb6b6, 15 regresiones OK. Incluye los dos consumidores de la prueba de vacío. |
| `20261105-094619-ac776c` | 1 | Baseline exacto con las 16 regresiones finales, 14 fallos y un error; control limpio pasa. Además los dos escenarios N-c fallan por las mismas causas de la review. `RESULT r4=1 fixtures=1`. |
| `20261105-094905-27d0a2` | 1 | Primera suite completa sobre 66c0d30. 76 tests, tres fallos de fixtures anteriores: SIGKILL sin recoger al runner zombie, reconciliación manual ante un barrido inconcluso y espera de 10 s del recibo final. Las 16 regresiones nuevas y W11/W12 pasan. |
| `20261105-100358-2b17ec` | 1 | 76 tests, 75 pasan. Los tres fixtures corregidos pasan; W11 encuentra incertidumbre legítima durante el cierre de su guion vacío. `3276f67` reintenta sólo ese control positivo con la operación real y exige además que cada escritura posea el mutex. |
| `20261105-101445-fb8398` | 0 | Aceptación sobre e9271df. Dos escenarios N-c OK, 76 tests OK en 529.924 s, Python 3.12.3, CPUs [0, 1], hash caac735a y `bin/pao check` correcto. |
| `20261105-102402-4ac3c9` | 0 | Segunda aceptación con exactamente los mismos 22 archivos de entrada. Dos escenarios N-c OK, 76 tests OK en 410.703 s y `bin/pao check` correcto. |

Los logs completos viven en `evidence/<runId>.log`. Sólo se quitan espacios al
final de línea. El baseline se genera con `git archive 568ec76`; se copian
encima únicamente `tests/test_r4.py` y el adversario de fixtures.
`evidence/r4-before.sh` corre ambos y conserva sus rc separados.
`evidence/r4-full.sh` corre la suite, el adversario, `bin/pao check` y
ShellCheck si existe. No hace falta instalar el módulo para repetirlos.

SHA-256 final de los siete archivos de `store.SOURCE_FILES`:
`caac735af226b3bca4eff0cf8d297313b52e153ea4f00e58b6bc66745abf14d0`.
El baseline imprime `0a7dd19b0967341e73e2319d43a98b4b628a4f3e232af5c42badd7e1b20304c2`.
Ambas aceptaciones imprimen Python 3.12.3, CPUs `[0, 1]` y el mismo hash.
Los gates internos midieron carga 3.7134 y 211.414 s, y carga 3.4766 y
156.959 s desde el último arranque ajeno. ShellCheck no está instalado y
su paso condicional se omite. La suite sí incluye sintaxis Python 3.9 y shell.
Una inspección final de `/proc/*/stat` y `/proc/*/cwd` por SSH devolvió
`LIVE_MIRROR_PROCESSES []` para ambos mirrors propios. `git diff --check`
pasa. Los commits posteriores a e9271df sólo documentan ejecución y evidencia;
no cambian runtime ni pruebas.

Archivos r4: `fleet/contracts/desk-politica-v1.md`, `fleet/deskpolicy/{cli.py,
engine.py,store.py,README.md,DELIVERY.md}`, `tests/{test_policy.py,test_r2.py,
test_r3.py,test_r4.py,desk-run.sh}` y los scripts y logs r4 de `evidence/`.
No hay preguntas de producto abiertas. El historial anterior se conserva abajo.

---

# Entrega U1, corrección para ronda 3

Felix quiere más corridas nocturnas en el desk, con reparto variable entre
Jale y Hasito y reservas que no pierdan resultados ni capacidad tras fallos.
La corrección parte de `d12722acb827ad841a0aff56288a4c8d5575d449`, comprobado
contra origin después de fetch. Se usó la rama existente en esta worktree
con `git switch --ignore-other-worktrees`, sin editar la otra worktree.

Se leyeron los briefs U1, r2 y r3, los diseños normativos y las reviews r1/r2.
Nivel alto por contrato entre servicios. Se siguió Bug fix: reproducción,
pruebas primero, corrección y verificación en el mismo Linux. No hubo
subagentes, PR, despliegue ni escrituras en las dos rutas prohibidas.
La review independiente y el APTO operativo corresponden a PAO.

## Cambios

- Recibos iniciales, replays y terminales se publican dentro del mutex. Un
  replay relee el recibo vigente, conserva la identidad admitida y nunca
  pierde una salida o causa conocida. La claim cerrada y el fd de vida libre
  quedan visibles juntos al soltar el mutex.
- Una vez recogida la salida del guion, los errores de cierre se diagnostican
  con `pao/desk-error@1` por stderr y el wrapper devuelve esa salida. Una
  segunda toma del mutex procura persistir incertidumbre, salida y
  `close_error`. Si falla escribir la claim pero el recibo es escribible,
  se conserva allí el resultado. Sin mutex no se escribe ninguno de ambos.
- `reconciliar` conserva las reservas con sesiones vivas o flock tomado.
  Sólo prueba vacío completo, sin eximir al supervisor original. Registra
  `reconciled`, sin perder peso, CPUs, recurso ni salida conocida. Los
  metadatos de slots Jale cerrados se archivan por rename para no aparecer
  después como carga legacy. No se toca el inode del flock. El primer cierre
  desde un keeper registra su sesión opcional `keeper_sid`, que también se
  comprueba al reconciliar. Cada publicación ejecuta reconciliación bajo la
  misma toma del mutex; desactivar noche persiste antes de comprobar el censo.
- `admitir --lane any` elige el menor carril permitido sin claim no cerrada
  de su proyecto. El wrapper lo abre bajo el mutex. La CLI de bajo nivel
  acepta un mapa `--life-fds LANE:FD ...` heredado del supervisor para retener
  el flock elegido después de que termine la CLI. Jale conserva carril
  concreto y fd 8; el contrato exige probar los carriles en orden y pasar al
  siguiente ante `resource-busy`, sin limpiar slots inciertos.
- El contrato permite el bootstrap idempotente de raíz y locks por `estado`,
  sin crear claims, solicitudes ni políticas. Incluye las tres obligaciones
  de U2: cola del mismo hilo antes de verify, allowlist de symlinks para
  `db:gen` y probes de `/proc` o `fuser -m` en vez de `lsof +D`.

## Hallazgos y pruebas

Las regresiones están en `tests/test_r3.py`. Los commits `b4c5315`, `de52363`, `c26c837` y `7b69d6a` registran las
pruebas antes de los arreglos. Las dos regresiones
C1 de r2 ahora dejan un hijo vivo: una reserva realmente vacía ya tiene el
camino normativo de reconciliación. Sus hijos pasivos se fijan a CPU 0,
aunque el censo de prueba contabilice otras CPUs. No se desactiva la
reconciliación para C1.

| Hallazgo | Commit de corrección | Fallo antes y resultado exigido después |
| --- | --- | --- |
| W11 / W-C | `221108d`; contrato `ae78fc3` | `w11_replay_never_overwrites_terminal_receipt`: antes archivo `(active, None, None)` frente a claim cerrada; después `(closed, 0, completed)`. `wc_census_during_final_receipt_has_no_untracked_lock`: antes `untracked-life-lock`; después `estado` y `publicar` concurrentes pasan. |
| W12 / N-2 | `221108d`, `efa0498` | `w12_real_close_mutex_timeout_preserves_exit`: flock real retenido 6 s; antes wrapper 75, salida null; después wrapper 3, `script_exit_code=3`, claim uncertain y peso 1. Casos separados de `process-owner`, OSError y excepción inesperada conservan salida y diagnóstico. También se prueba un fallo al persistir la incertidumbre. |
| W-A | `e6811bc`, `efa0498` | `wa_reconcile_dead_day_claim_restores_night_cpus`: antes queda abierta; después día caído cierra como reconciled y admite noche. `wa_reconcile_keeps_child_and_life_lock_then_closes` conserva al hijo vivo y archiva metadatos sólo tras su fin. La CLI manual se prueba con la sesión viva de un keeper y después de su fin. |
| W-B | `e6811bc`; contrato `ae78fc3` | Antes `any` falla en argparse; después el carril 1 incierto conduce al 2. Se prueba tanto el wrapper como `admitir` con fds candidatos heredados, reteniendo el flock 2. |
| N-1 y notas U2 | `ae78fc3` | Antes faltan las reglas documentales; después la regresión de contrato exige bootstrap idempotente, ausencia de claims nuevas y las tres notas. |

No se rebaten hallazgos aceptados. No se implementa Jale ni Hasito, ni se toca
hostgate. `--life-fds`, la sesión del keeper y el archivo de metadatos precisan
cómo cumplir los requisitos existentes; no amplían los presupuestos.

## Ejecuciones

Las pruebas corrieron en el desk mediante `REMOTE_RUN_HOST=desk remote-run`,
una corrida propia a la vez, con el runner bajo `taskset -c 0-1`. Antes de cada
start se comprobaron carga menor de 9 y 120 s desde el último `started`
ajeno, con fallback al mtime del directorio. El guion repite el gate dentro
de la corrida. Los fixtures viven bajo el mirror. La MacBook sólo editó y
preparó snapshots y hashes. No se ejecutaron instalador ni retirada.
En la primera suite completa, los hijos pasivos añadidos a C1 podían recibir
CPUs contables fuera de 0..1. Se corrigió el fixture en `61385e0` para fijarlos
a CPU 0; la suite final incluye esa corrección. La primera corrida no se
presenta como aceptación final de la restricción de afinidad.

| RunId | rc | Evidencia |
| --- | --- | --- |
| `20261105-082031-f71d5c` | 1 | Primer repro: 8 casos, 7 fallos y 3 errores. Reproduce recibo degradado, timeout con 75 y censo con `untracked-life-lock`. Los subcasos posteriores al primer fallo de N-2 quedaron afectados por el fixture y no cuentan como prueba individual de OSError o excepción inesperada. |
| `20261105-082538-5fadbb` | 1 | Primera corrección: 11 casos, 2 fallos de fixture por renovar justo al TTL en las pruebas de `any`; los otros 9 pasan. Se corrigió la renovación a pulsos de 30 s. |
| `20261105-082810-16cef4` | 1 | Snapshot exacto del runtime d12722a, con las 11 regresiones corregidas: 6 fallos y 5 errores. Esta reproducción sí aísla los tres fallos de cierre en fixtures nuevos. |
| `20261105-083041-ddbace` | 0 | Primera suite completa sobre ae78fc3: 57 pruebas OK en 318.179 s y `bin/pao check` correcto. |
| `20261105-083903-d6ab75` | 1 | Runtime d12722a con las 14 regresiones finales: 8 fallos, 5 errores y un pase (guard de desactivación que ya cumplía r2). El censo concurrente reproduce `untracked-life-lock`. |
| `20261105-084604-8eb1df` | 0 | Suite final sobre `61385e0`: 60 pruebas OK en 348.597 s, sintaxis Python 3.9 y shell, y `bin/pao check` correcto. Afinidad del runner `[0, 1]`; hijos pasivos C1 corregidos a CPU 0. |

Los logs completos viven en `evidence/<runId>.log`. Se quitan sólo espacios
al final de las líneas. `evidence/r3-regressions.sh` conserva el comando del
repro acotado. El baseline se genera con `git archive d12722a` y se copia
encima únicamente `tests/test_r3.py`; no se modifica su runtime ni contrato.
Las faltas de comandos nuevos producen errores esperados en ese baseline.
No se usan esos errores como sustituto de las aserciones de resultado final.

SHA-256 del módulo final (los siete archivos de `store.SOURCE_FILES`):
`0a7dd19b0967341e73e2319d43a98b4b628a4f3e232af5c42badd7e1b20304c2`.
El baseline conserva el SHA-256 r2
`92b0957b76e2c33b63dcd49f49b73193d26566c4978423a78cd28bfc35b60bae`.
El gate interno final midió carga 1.8828 y 199.855 s desde el último
arranque ajeno. El hash impreso por Python 3.12.3 coincide con el anterior.
El timeout real del mutex imprime `wrapper_rc 3`, `script_exit_code: 3`,
`claim_state: uncertain` y un diagnóstico `mutex-timeout` separado.
Las 14 regresiones finales pasan, incluidos ambos censores concurrentes.
Después de la suite, una inspección de `/proc/*/stat` y `/proc/*/cwd` por SSH
no encontró procesos no zombies con cwd bajo el mirror final:
`LIVE_MIRROR_PROCESSES []`. `git diff --check` también pasó.
Los commits posteriores al probado sólo registran esta entrega y sus logs;
no cambian runtime ni pruebas.

Archivos r3: `fleet/contracts/desk-politica-v1.md`; `fleet/deskpolicy/{cli.py,
engine.py,README.md,DELIVERY.md}`; `tests/{test_policy.py,test_r2.py,test_r3.py,desk-run.sh}`
y los logs y el guion de reproducción de `evidence/`.
No quedan preguntas de producto abiertas. Los gates de consumidores y el APTO
siguen en PAO, U2 y U3. La evidencia de ronda 2 se conserva abajo como historial,
no como estado vigente de esta entrega.

---

# Entrega U1, corrección de ronda 2

Felix quiere usar el desk de noche para más corridas en paralelo. El reparto
entre Jale y Hasito varía según los turnos en espera y lo gobierna un archivo
de política PAO con vencimiento. El canario conserva 4 corridas y peso CPU 8.

Nivel alto por contrato entre servicios y reserva común. Se aplicó felix-mode
con el playbook Bug fix. No hubo descendientes, PR ni despliegue. La revisión
independiente posterior y la apertura del PR corresponden a PAO.

## Base y alcance

Se leyeron completos el brief U1, el brief de corrección r2, ambos diseños
normativos y las reviews sol y opus de ronda 1. El remoto y el checkout se
comprobaron en `dca130f5a850f210be1c6d10a2880bdfb1631c8a` antes de editar.
La rama local ya existía en la worktree anterior, por lo que el comando
`switch -c` falló. Se usó `git switch --ignore-other-worktrees feat/desk-politica-v1`
en esta worktree propia. No se editaron archivos de la worktree anterior.

La reserva sigue dentro del registro atómico de solicitud. El cambio de
ciclo de vida añade token de handoff, salida del guion y causa de cierre.
Model the Domain conservó separados la decisión de admisión y el resultado
del guion. Una claim incierta conserva recurso, CPUs y peso.

## Hallazgos y regresiones

Las pruebas nuevas están en `tests/test_r2.py`. Los nombres de la tabla
omiten el prefijo `test_`. El commit `eacea1a` registra primero los repros.
La documentación normativa común quedó en `adcc570`.

| Hallazgo | Commits de corrección | Regresión |
| --- | --- | --- |
| C1 | `99dac62`; fixtures en `5a93bd2` y `ddd3c9f` | `c1_uncertain_resource_stays_reserved_for_both_projects`, `c1_death_after_census_before_flock_keeps_active_reservation`, `c1_estado_reports_duplicate_without_changing_records`. Jale y Hasito conservan el recurso, sin claim nueva; otro carril sigue siendo admisible. El duplicado inyectado sigue visible como error 75 y no cambia archivos. |
| W1 | `7700117`, `b4ae402` | `w1_empty_boot_creates_private_state_and_admits_day`; la prueba W9 admite por CLI heredando el mutex. Raíz y subdirectorios 0700, mutex 0600, día sin política. |
| W2 | `30832a7`, `b3c54a6`, `6bf0695` | `w2_remote_run_0775_receipt_is_accepted`, `w2_invalid_receipt_preconditions_never_commit_claim`. Carpeta 0775 admitida; carpeta 0777 y recibos conflictivos o incompletos dejan cero claims. |
| W3 | `f6b1c24`, `87778b5` | `w3_script_exit_is_preserved_with_live_descendant`, `w3_stdout_reports_final_receipt_without_receipt_file`. Un guion sale 3 con hijo vivo; rc 3, recibo admitted, salida 3 y claim incierta con capacidad retenida, tanto en archivo como en el recibo final de stdout. |
| W4 | `8d7a53b`, `593cda9` | `w4_remote_runner_group_term_reaches_script_and_closes`, `w4_remote_runner_group_kill_reaches_script_and_closes`. TERM y KILL del grupo alcanzan al guion; salida 143 o 137 y cierre `stopped`. |
| W5 | `24f7be5` | `w5_keeper_closes_in_own_session_with_inherited_token`. El token incorrecto falla; el keeper en otra sesión con fd de vida y token heredados cierra. La prueba W9 pasa el token por fd a la CLI real. |
| W6 | `29367ec`, `adcc570` | `w6_expired_window_modes_and_documented_writer_temporary`. Ventanas vencidas legibles en 0600, 0644, 0664 y 0777 son inertes; un temporal `.pao-window-*` incompleto no invalida el censo. |
| W7 | `c07e4f8`, `9703c51` | `w7_oversize_rejected_before_fifo_and_reduction_expires_head`. Pesos 10 y 13 dan 75 `weight-too-large` sin solicitud. Reducir el máximo retira de FIFO una cabeza excesiva y permite al siguiente. |
| W8 | `c7752c7`, `adcc570` | `w8_day_projects_are_independent_of_weight`. Dos Jale y dos Hasito con suma 14 entran de día y reciben 12 CPUs. La suite original conserva presupuesto y afinidad nocturnos. El contrato fija la tabla de comandos y Turbo concurrency 4 para U2. |
| W9 | `aea1af2`, `b4ae402`, `f0713ca` | `w9_explicit_cli_harness_reaches_real_entrypoint`. CLI real con raíz, reloj, boot, loadavg y ruta de listeners privados; carga 9 rechaza Jale. Admisión y cierre con fds reales pasan. Sin la llave del harness no se aceptan costuras ni código fuera de instalación. |
| W10 | `a0ca557`, `f0713ca`, `dc16bae` | `w10_cutover_prevalidates_historical_locks_and_is_idempotent`, `w10_failed_publication_rolls_back_and_retry_preserves_state`, `w10_concurrent_destination_change_is_revalidated_under_mutex`. Locks 0644 fallan antes de instalar. Tras chmod conservan inodes; instalación repetida idéntica, rollback de error inyectado, reintento sin reemplazar evidencia y rechazo de un destino concurrente distinto bajo el mutex. |

No se rebaten C1 ni W1–W10. Los cuatro nits se declinan como manda el brief.
No se añadió recolección de tombstones, liberación forzada de inciertas ni
cambios a hostgate. Los cambios del wrapper resuelven W4.

## Pruebas en el desk

Todas las corridas usan `REMOTE_RUN_HOST=desk remote-run`, una a la vez,
`taskset -c 0-1`, gate previo de carga menor de 12 y 120 s desde el último
cambio de carpeta de runs. El guion repite los gates al arrancar.
Todos los fixtures viven en el mirror. La MacBook sólo editó código.
No se escribió en `/tmp/sen-test-slots` ni en estado comprador real.
Las pruebas del instalador llaman al helper con destinos privados de fixture.
No ejecutan `deploy-desk.sh` ni `withdraw-desk.sh`.

| RunId | Artefacto | rc y resultado |
| --- | --- | --- |
| `20261104-234756-08ee37` | Runtime original, pruebas de `eacea1a` | 1. Las 28 originales pasan. Total 42; 10 fallos y 5 errores en las regresiones nuevas. |
| `20261104-235729-570bad` | Runtime y pruebas de `f0713ca` | 0. 44 pruebas OK en 960.836 s y `bin/pao check` correcto. |
| `20261105-001608-85e3f9` | Intento sobre `937621c` | 1. Gate interno de espaciado, sin ejecutar pruebas. |
| `20261105-002203-3da4ce` | Runtime y pruebas de `be5b12a` | 0. 44 pruebas OK en 431.592 s y `bin/pao check` correcto. |
| `20261105-003127-75c302` | Runtime y pruebas finales de `a9de766` | 0. 46 pruebas OK en 449.386 s y `bin/pao check` correcto. |

El primer gate midió carga 11.0703 y 339.08 s. El segundo gate midió carga
9.2910 y 153.22 s. Antes del intento final se midieron carga 8.6655 y
149.05 s, pero el gate interno rechazó el espaciado respecto de otra carpeta
de runs. Se conserva el intento fallido y se reintenta sin relajar el gate.
El gate de la segunda corrida corregida midió carga 5.7480 y 145.18 s.
El gate final midió carga 4.8789 y 131.03 s. La corrida final acreditó Python
3.12.3 y afinidad `[0, 1]`. Su hash de los siete archivos del runtime es
`92b0957b76e2c33b63dcd49f49b73193d26566c4978423a78cd28bfc35b60bae`.
Los commits posteriores sólo registran documentación y evidencia, sin cambiar
ese runtime ni las 46 pruebas. Una consulta final de `/proc` no encontró
procesos no zombies con cwd en el mirror de esta corrida.
Los logs completos están en `evidence/<runId>.log`. Sólo se normalizan
los espacios al final de líneas de progreso de unittest para `git diff --check`.
Los errores de la reproducción incluyen el helper de instalación y el API de
token aún ausentes. La primera versión del fixture de carrera llamaba desde
una sesión no válida; `5a93bd2` la ejecuta en una sesión real y cubre la muerte
entre censo y flock. No se presenta el error anterior de fixture como prueba
de esa variante de C1. La reproducción de claim incierta sí mostró admisión
indebida y `duplicate-resource` con el runtime original.

La suite completa incluye además sintaxis Python 3.9, sintaxis de los tres
guiones, `bin/pao check` y ShellCheck si está instalado.
Los gates e integración final de Jale y Hasito siguen en sus unidades.

## Corte y regreso

El contrato documenta el drenado, mutex histórico, chmod de los mismos inodes,
archivo de metadatos sin borrar, instalación de día, evidencia, armado y canario.
El instalador prevalida antes de publicar runtime o configuración. Una
instalación idéntica es reejecutable y un error detectado devuelve los nombres
nuevos al staging. Las pruebas inyectan un error después del commit de
configuración y acreditan rollback y reintento. No simulan un corte eléctrico.

El regreso publica día, conserva todas las reservas y drena antes de revertir
consumidores o binarios. El módulo no recupera por TTL una claim incierta.
`taskset` no limita Docker Desktop ni trabajo que escape deliberadamente.
La noche requiere política válida de PAO, preflight ligado al boot y código,
y arranque explícito del escritor. El día se recupera de una raíz ausente.

Los esquemas r2 añaden campos obligatorios a la claim y al recibo. El corte
exige drenado y consumidores coordinados. No hay migración automática de
claims de ronda 1 ni adopción de carga legacy. No quedan preguntas de producto
abiertas para esta corrección. PAO conserva el APTO operativo y la activación.

## Archivos cambiados respecto de la ronda 1

- `fleet/contracts/desk-politica-v1.md`.
- `fleet/deskpolicy/README.md` y este `DELIVERY.md`.
- `fleet/deskpolicy/cli.py`, `engine.py`, `model.py`, `store.py`, `testing.py`.
- `fleet/deskpolicy/installation.py` y `deploy-desk.sh`.
- `fleet/deskpolicy/tests/test_policy.py` y `tests/test_r2.py`.
- Logs de las corridas de ronda 2 en `fleet/deskpolicy/evidence/`.
