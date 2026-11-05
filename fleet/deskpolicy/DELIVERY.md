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

El código probado está en `dcb6e50`; los commits siguientes documentan evidencia.
SHA-256 de los siete archivos de `store.SOURCE_FILES`:
`77a369608532867e785aa7eff8f496e9a2480ff7a3b8d3b98a4d826901e7200d`.
`evidence/r5-inputs.sha256` fija los 22 archivos de runtime, pruebas, contrato,
README y scripts de aceptación. Su hash es
`646578e5eaf827a99f7fe78f849cd87f1e067bce68e34efb8636e9915abc35a0`.
Las dos copias remotas verifican los 22 hashes sin diferencias. Los resultados
están en `evidence/r5-inputs-first.log` y `evidence/r5-inputs-second.log`.
La suite imprime Python 3.12.3 y afinidad `[0, 1]`.

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
