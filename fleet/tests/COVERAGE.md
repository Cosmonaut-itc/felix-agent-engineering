# SEN-3820 harness r7 — matriz pública, cero ejecución del candidato

Inventario obtenido por texto, sin discovery/imports: **64 métodos** en un único
TestCase `FleetCLI`:16 base en `test_cli.py`,28 en `Core3820Cases`,20 en
`Operator3820Cases`. Se conservan íntegros los62 métodos de d5 (16+28+18), sus
contratos; r4 añadió dos y r5 corrigió sólo el oráculo temporal del
primer método r4, sin añadir métodos. R7 conserva r6 y corrige sólo retención
temprana, fotografía C08 y respuesta lectora C07, sin nuevos métodos.
Los53 r1, cinco r2, cuatro r3 y refuerzo C02
quedan conservados. Discovery futuro sólo `test_cli.py`, según README.
**34 filas padre =26 definidas +6 parciales/condicionales +2 positivas abiertas**.
Los3 requisitos adicionales no cambian esa partición. No se cuentan métodos como
subcasos ni código escrito como cobertura runtime. R7 sigue sin ejecutar; los
resultados históricos de c63 se distinguen al final.

Cada nombre siguiente es un método real completo, con prefijo `test_`. C reside en
`core3820_cases.py`, O/A/B en `operator3820_cases.py`. Recursos comunes: directorio
físico propio0700 `FLEET_TEST_RUN_BASE`, estado/proyecto/HOME propios, CLI en procesos,
JSON/códigos originales, transiciones y barrera con watchdog fallido30s. El comando
counted escribe/fsync un contador externo fuera del origen capturado. IDs iguales
por sí solos no prueban una ejecución. Sólo procesos de fixture reciben señales.
Ninguna prueba importa Fleet, escribe SQL o inyecta respuestas/cierre en producto.

| Fila / clasificación frozen | Método (prefijo test_) | Subcasos y oráculo escrito; límite |
| --- | --- | --- |
| C01 / definido | c01_equal_concurrent_admission_and_external_single_entry | Dos clientes con barrera externa de inicio: new/replay, IDs/admission/input originales, contador1, wait0 tras final. Carrera acotada; no acredita interleaving interno determinista. |
| C02 / definido | c02_different_concurrent_content_conflicts_without_second_entry | r2: correspondencia argv/etiqueta/CompletedProcess/respuesta0/5; existing original y rechazo explícito. Contador externo exactamente [etiqueta ganadora] durante running y al terminar. Stdout y artifact winner.txt publican sólo esa etiqueta; evidence/manifiesto y hashes ligados a job/attempt/input ganadores. Carrera acotada, sin ganador predeterminado ni intercalación interna acreditada. A5 corrige el oráculo, no demuestra ejecución errónea r1. |
| C03 / definido | c03_discarded_submit_stdout_recovers_by_key_and_replays_once | Toda salida submit a DEVNULL; otro status K atribuye admisión, replay exacto conserva IDs, contador1. ACK perdido, no caída en commit. |
| C04 / definido | c04_lookup_after_origin_removed_and_candidate_rejection_is_scoped | Origen retirado tras running; status/wait/evidence con env diferente resuelven original; nuevo candidato imposible2/not_evaluated, asociación conservada. |
| C05 / definido | c05_finished_success_and_failure_replay_never_execute_again | Exit0 y7 separados: wait0/1 con complete; replay conserva recibo y contador1. |
| C06 / definido | c06_lost_supervisor_and_empty_group_do_not_prove_terminal | SIGKILL a supervisor propio tras running, gate termina comando, ausencia de grupo observada; A incierto, B bloqueado owner_uncertain, replay0, wait/recover3, sin otra entrada. |
| C07 / definido | c07_sigint_wait_only_interrupts_reader | Shim propio restablece y comprueba SIGHUP/SIGINT=SIG_DFL antes de exec auténtico, con marcador. Running en barrera; importtime acredita sólo importación. SIGINT real exige130/client_interrupted, operation=wait y error.effects=read_only en5s: descarta catcher exterior unknown/unknown y acredita identificación de operación lectora en main, no entrada exacta al bucle wait. Historia/revisión/capacidad/dispatch iguales y contador1; no acepta0 ni alarga timeout. |
| C08 / definido | c08_client_group_loss_preserves_original_supervisor | Mismo shim exclusivo de clientes C07/C08, wrapper en sesión propia. self.ready("c08", ready) exige marcador y running público durable antes de fotografiar admitted y enviar SIGHUP real al grupo; marcador y retorno por SIGHUP en5s. Supervisor original, running, reserva/transiciones y contador1; settle10s. Sin preexec_fn ni política global. |
| C09 / definido | c09_timeout_only_for_safe_pending_and_uncertainty_precedes_it | Running da4; dueño perdido da3 antes de timeout0. Ventanas terminal/publication/release pendientes sólo si se alcanzan externamente (C20/O07); no acreditadas aquí. Baseline wait registra/reintenta únicamente la ventana finished/publication pending para esperar complete. |
| C10 / definido | c10_enqueue_only_survives_and_two_recoverers_claim_once | Enqueue-only queued seguro y dispatch_not_requested; replay no despacha; dos nuevos recoverers, un claim durable/contador1. Se tolera3 transitorio de un observador concurrente starting sin vínculo. |
| C11 / definido | c11_results_survive_new_coordinator_processes | Exit0/7, procesos nuevos status/evidence/recover; comando/huella/IDs persistidos, wait0/1, contador1. |
| C12 / definido | c12_recover_preserves_live_owner_and_does_not_adopt_lost_owner | Dueño vivo acreditado permanece, recover no claim; variante supervisor perdido retiene/recover3. Pérdida del coordinador separado también en C08. |
| C13 / definido | c13_effective_bytes_catalog_mode_head_and_index | Repo propio: bytes, catálogo y bit exec distintos dan5; sólo índice diferente replay; commit vacío cambia HEAD y da5. Contador externo no aumenta. |
| C14 / definido | c14_private_exclusion_secret_equality_and_undeclared_negative | Privado mutado replay; credencial ficticia cambiada5 sin valor/digest público; archivo ordinario no declarado cambia conflicto y sí viaja al log; stdout/argv con secreto declarado se redactan. |
| C15 / definido | c15_argv_environment_outputs_declarations_client_and_pwd | Variaciones separadas argv/PATH/volátil/presencia env/artifact/credential-env/client dan5; sólo PWD derivable replay; PWD declarado credencial cambiado da5. |
| C16 / definido | c16_precommit_rejection_does_not_bind_key | Autoridad inicializada; input inválido2/candidate false/not_evaluated, status K not_found con revisión; después K válido new. |
| C17 / definido | c17_absence_is_revision_and_authority_scoped | Not_found antes de envío y admisión posterior con revisión mayor; lector no inicializa otra autoridad; mismo K nuevo en otra autoridad tiene UUID distinto. La ausencia no pronostica una carrera futura. |
| C18 / definido | c18_missing_artifact_signal_and_positive_never_created | Exit0 sin artifact: cierre original, collection incomplete, retained, wait/recover3 y replay histórico; señal propia SIGKILL conserva signal/no exit137 y wait3 aun released; ejecutable inexistente: never-created positivo, bundle vacío explícitamente N/A, complete/failed/wait1. |
| C19 / definido | c19_post_completion_payload_loss_does_not_rewrite_history_or_reacquire | Retirar artifact después de complete; sustituir manifest de un segundo job por el primero, conservando copias: recorded succeeded, outcome incomplete/evidence false, wait3 y released permanece. Recover no readquiere capacidad. |
| C20 / condicional BLOCKED | c20_real_quota_failure_at_terminal_collection | R6 prepara sólo setup pao-cs certificado, raíz propia e inventarios; termina BLOCKED antes de admisión/gate/relleno porque falta discriminante aprobado de colección/publicación terminal y ack terminal propio. ENOSPC/EDQUOT del relleno, gate fallido o incertidumbre no acreditan esa frontera. Una implementación futura aprobada debe conservar retención/wait/recover3/contador1 y restaurar exclusivamente relleno propio atribuido en finally. Ningún PASS/skip por setup. |
| C21 / ABIERTO positivo | c21_host_backend_rejected_without_claiming_positive_generation_coverage | Sólo submit backend host-reservation@1 rechazado2/backend_unsupported/not_evaluated, sin crear estado. Falta integración capaz de entregar/procesar cierre ordinario G con H activo. No API simulada ni test positivo. |
| C22 / definido | c22_readers_and_replay_leave_original_authority_unchanged | Running en barrera y luego supervisor perdido con grupo terminado; status/evidence/wait/lector fallido con TMPDIR/TEMP/TMP apuntando a estado y alias; inventarios sin atime idénticos en ambas vistas. Replay con env original conserva transiciones, payloads originales y contador; permite sólo preparación propia atribuible. |
| C23 / parcial/condicional | c23_authentic_incompatible_cli_states_and_retention_preconditions | CLI auténticos2/3 en copias propias crean estados activos y cerrados; nuevo status por JOB/K, wait, evidence, recover, submit con/sin K rechazan2/unsupported_schema con inventario sin cambios. Historial auténtico>7d consulta K y fecha real; sin reloj inyectado/SQL. Faltan copias CLI provistas e historial auténtico; edad uncertain separada no acreditada por un historial finished. |
| C24 / parcial | c24_public_queued_and_running_crash_campaign_preserves_one_permission | Queued público, recover, running, replay y contador1; supervisor propio perdido tras running retiene/recover3. C08 añade muerte de cliente. Claim/vínculo/compuerta/Popen/fsync/commit exactos no controlados ni cubiertos por esos estímulos. |
| O01 / definido | o01_positive_local_resolution_keeps_failed_evidence_and_drained_fifo | Cierre original real con artifact faltante; B queued; drain/assess/release auditados, actor nativo, proof, errores e incomplete conservados, drained y B sin entrar. Sólo reopen+recover permite contador B1. |
| O02 / definido | o02_empty_group_without_original_terminal_and_self_assertions_cannot_release | Supervisor perdido y grupo terminado; assess insuficiente3/eligible false, proof inventado2, proof negativo3 y --proof-file JSON rechazado2. PID/TTL/done/contención autoafirmados no autorizan release. |
| O03 / condicional | o03_reason_and_declared_actor_rejections_with_real_os_actor | Motivo ausente/vacío/espacios/control/largo y flags --actor/--uid rechazados2; USER ficticio no cambia uid/cuenta/birth nativos. Otra cuenta real y denegación real de observación nativa requieren ejecutores externos preexistentes: no hay fixtures equivalentes ni cobertura positiva de esos negativos. |
| O04 / definido, operador | o04_new_old_generation_release_is_processed_and_rejected_before_proof | G liberado, H running, C queued, drain nuevo; nueva operación para G da5/generation_mismatch, validation_processed auditado, H intacto. Replay G devuelve historia sin tocar H; C sólo tras cierre/reopen/recover. No cierra C21. |
| O05 / definido | o05_operation_binding_replay_conflict_and_concurrent_release | Dos releases mismo ID convergen new/replay con receipt igual; motivo distinto5/audit; dos IDs distintos de otra reserva producen committed/already_released, un release efectivo. |
| O06 / condicional BLOCKED | o06_real_quota_failure_cannot_commit_capacity_without_audit | Dos subTest de setup propio, release y reopen, ambos BLOCKED antes de admisión/relleno. No se simula assess/release. Falta discriminante aprobado de fallo real en commit tras assess y tras release respectivamente; rc3/persistence_unconfirmed/operation_not_found puede venir de apertura y nunca basta para PASS. Mantiene como obligación futura rechazo2/3 y status not_found/retención o committed con auditoría completa/capacidad/control correlacionados, contador1 y recuperación. No inyección ni observación interna fabricada. |
| O07 / parcial | o07_lost_stdout_recovers_each_atomic_operator_binding | Descartar stdout de los4 verbos, status y replay recuperan receipt; segunda asignación terminal, SIGKILL a clientes propios después de importtime y consulta/repetición explícita. Registra si señal se envió y binding observado. Frontera exacta intención/commit/fsync no probada; importtime/ACK no se presentan como tal. |
| O08 / definido | o08_only_explicit_current_epoch_reopen_allows_dispatch | Release incompleto sin reopen; evidence/recover no abren ni B entra. Reopen explícito; nuevo drain D2, replay viejo devuelve historia, nueva operación D1 da5. Reopen D2+recover permite B1. |
| O09 / definido | o09_old_epoch_proof_and_cross_authority_proof_cannot_change_current | Tras release/reopen/drain nuevo, misma asignación ya libre responde already_released sin repetir release. Proof de otra autoridad contra destino todavía retenido da2/proof_invalid; proof propio sí libera. Mismatch prioritario con H en O04. Proof_stale exige cambio relevante acreditado, no se simula alterando revisión. |
| O10 / ABIERTO, fuera3820 | o10_host_side_operator_backend_is_explicitly_unsupported | Sólo operator_backend_unsupported2 auditado, sin drenar ni soltar. Positivo host-side depende de dueño/interfaz/prueba autorizada futura3822/3823; sin fixture/API ficticia. |

## Requisitos adicionales

| ID | Método (prefijo test_) | Oráculo y parte pendiente |
| --- | --- | --- |
| A-W1 | a_w1_live_drain_blocks_reopen_and_ordinary_supervisor_handoff | A running causal, B queued → drain D → reopen prematuro3/reopen_blocked → cierre ordinario A completo/released, B permanece queued durante D → reopen nuevo+recover → B una vez; audit/transiciones/counters. A6 diferido: released no acredita que el supervisor haya recorrido el relevo antes de reopen; no cierra cobertura causal de esa guarda. |
| B-W1 | b_w1_sigint_dispatch_campaign_records_actual_observable_window | Tres submits propios, observar admisión/estado/vínculo, SIGINT sólo si cliente vive, salida0/130 y admisión durable; gate, terminal/released y contador≤1. Registro explícito exact_pre_spawn_proven=false. Starting/unbound no prueba que Popen no ocurrió. Falta control externo causal del hueco preciso claim→spawn, sin hooks/SQL/sleeps que lo finjan. Política de producto sí está implementada y descrita en SEN-3820.md. |
| B-W2 | b_w2_atomic_local_binding_not_found_committed_ack_loss_and_conflict | Status not_found, drain con stdout perdido, committed/audit, replay receipt igual, cambio5; recover operator_pending vacío y drained, segundo ID already_drained0 misma época. Rollback/cuota en O06 y muerte acotada en O07; no pending durable en modelo local. |

## Regresiones r2 adicionales

Los nombres llevan prefijo `test_`. La trazabilidad estática exacta contra0b,
incluida la diferencia de A5, está en [SEN-3820-r2.md](../SEN-3820-r2.md).
Estas cinco adiciones no reclasifican las34 filas contractuales.

| Hallazgo / módulo | Método | Subcasos escritos y discriminante |
| --- | --- | --- |
| A1 / core | r2_a1_complete_selections_validation_and_true_equivalences | 6 estímulos tras setups: punto+foo5 sobre bytes iguales; automático/punto duplicado/reordenación con duplicados replay; punto+missing y punto+.env2/not_evaluated. Original/contador intactos. 4 invocaciones subTest. |
| A2 / operator | r2_a2_conflict_audits_entire_denied_request_without_rebinding | 3 conflictos con otra asignación auténtica (release/assess/reopen), todos los selectores/prueba/época distintos; request íntegra/redactada y5/efectos nulos. Motivo secreto2 auditado sin binding;3 backends malformados2 sin auditoría; replay G conserva receipt ante H retenida. 6 invocaciones subTest,8 estímulos de estas categorías. |
| A3 / operator | r2_a3_pages_exhaust_divergent_operations_and_audit | 1 recorrido: una operación+3 conflictos sólo audit, límite2, ambos cursores, orden/identidades/listas exactas, avance y fin; página vacía, selección exacta e inventario iguales. |
| A4 / core | r2_a4_reader_open_and_selector_failures_are_read_only | 19 estímulos:4 lectores ×2 aperturas (ausente/privacidad insegura),3 lectores ×3 selectores erróneos, operation_not_found y paginación inválida. Todos2/read_only/sin observation; no crear autoridad, inventario idéntico. 17 invocaciones subTest. |
| A4 / core | r2_a4_real_directory_lock_distinguishes_readers_and_writer | 4 lectores concurrentes bajo flock EX externo causal devuelven2/read_only/state_lock_timeout;1 escritor distinto conserva3/unknown. Gate/finally, recursos propios, watchdog60s, ausencia de binding e inventarios idénticos. Ningún ledger escrito por fixture. |

Cinco métodos nuevos y27 invocaciones `subTest` nuevas por expansión estática de
bucles; recorridos/aserciones sin subTest no son métodos adicionales. Todos escritos,
cero ejecutados. Esquema incompatible sigue en C23 condicionado a CLI auténticos;
corrupción SQLite real/EACCES de apertura no tienen nueva fixture en r2. Su manejo
se inspeccionó; no se fabrican bytes de ledger para cerrar esos escenarios.

## Regresiones r3 adicionales

Los cuatro métodos completos llevan prefijo `test_`. La respuesta individual,
fuentes127 y semántica pública están en [SEN-3820-r3.md](../SEN-3820-r3.md).
Los58 métodos anteriores y las34 filas padre conservan sus setups y oráculos.

| Hallazgo / módulo | Método | Subcasos escritos y discriminante contra127 |
| --- | --- | --- |
| R2-A01 / operator | r3_a01_new_denial_sanitizes_all_selectors_and_preserves_raw_binding | Credenciales ficticias de forma válida persistidas por submit enqueue-only; lector sin ese env; operation-id ausente y release NUEVO2/unknown_job. Ausencia de valores en ambos canales, target anidado/drain/proof redactados, generation int7 y atribución conservadas; receipt exacto, replay2 y recorrido completo limit1 iguales a listado grande (2 operaciones/5 eventos). Cambiar sólo un secreto por otro da conflicto5 pese a igual redacción; reason/ID secretos dan2 auditado sin binding, control no secreto conserva generation_mismatch5. Job/control/capacidad/contador sin efectos.127 publica los selectores crudos en la primera respuesta nueva: falla el oráculo de ausencia antes de comparar receipt. 2 invocaciones subTest de rechazo por credencial. |
| R2-A01 / operator | r3_a01_readers_resanitize_history_with_later_persisted_declarations | Productor CLI auténtico guarda una denegación antes de declarar sus selectores ficticios como credenciales; segundo submit enqueue-only los declara. Status exacto, página limit1/fin y replay deben ahora sanear esa historia sin reescribirla, preservando receipt atribuible, revisión y listas.127 devuelve history cruda: falla ausencia en status exacto. No SQL/mocks/ejecutable antiguo ni objeto guardado inyectado. Sin subTest. |
| R2-A02 / core | r3_a02_empty_inventory_automatic_and_dot_replay_without_dispatch | Origen realmente vacío y sin ancestro Git: automático admite input hash de []; punto, punto duplicado y automático replay0 aunque se omita enqueue-only. Punto+missing/.env/.././ sigue2/not_evaluated; sólo .env excluido mantiene inventario vacío y equivalencia. Otra declaración private conserva conflicto5 aun con mismos bytes vacíos; job/admission/input/safe-pending/control/transiciones intactos, sin contador/supervisor.127 rechaza el primer replay punto2/input_not_admitted. 9 invocaciones subTest (3+4+2). |
| R2-A03 / operator | r3_a03_both_cursor_ranges_return_json_without_mutation | Autoridad válida, una operación/dos eventos. Ambos flags con -1, int64max+1 y uint64max, tanto enumerando como por ID: JSON una línea2/invalid_pagination/read_only, sin traceback/auditoría/observation/mutación.0, max-1 y max válidos; exacto conserva receipt, default audit y streams independientes conservados, páginas normales y fin.127 desborda SQLite con max+1 enumerado en cualquiera de los flags: traceback1 sin JSON. 18 invocaciones subTest (12 inválidas+6 válidas); además recorrido independiente de2 páginas sin subTest. |

Cuatro métodos nuevos,29 invocaciones subTest nuevas por expansión estática
(A01=2, A02=9, A03=18). Son casos escritos; **cero ejecutados**, incluido baseline127.
Se mantienen las27 invocaciones subTest añadidas en r2 y todos los demás casos.

## Regresiones r4 adicionales — R3-A01

Dos métodos añadidos en r4 a `operator3820_cases.py`, ambos con prefijo `test_`, más el
helper de recorrido `r4_operator_pages`. R5 sólo modifica el primero de esos métodos.
Regla pública y fuentes completas en [SEN-3820-r4.md](../SEN-3820-r4.md).
El discriminante se deduce de leer d5 y el oráculo; no hay baseline FAIL ni PASS
medidos. La ausencia global sólo se exige para selectores secretos; credenciales
que coinciden con protocolo se comprueban por campo y procedencia.

| Método | Fixture CLI válida, oráculo y discriminante estático contra d5 |
| --- | --- |
| r4_a01_denial_literals_and_copied_data_before_and_after_declaration | Origen físico vacío noGit, counted externo, submit enqueue-only con declaraciones explícitas de fleet-job-api@1/unknown_job/no_capacity_change y seis selectores válidos. Dos subTest: declaración antes de la NUEVA denegación y después de historia auténtica (denegación+conflicto por CLI). Release de job inexistente llega a unknown_job antes de drenado/prueba. Respuesta/recibo/evento/replay/exacto/listado y todas las páginas limit1 conservan api/error.code/effects/decision/status/action/binding; target/drain/proof siguen redactados y generation int7. Un backend copiado igual al literal api se redacta en request del conflicto. Cambiar sólo j_A por j_B da5, aunque ambas proyecciones sean iguales; binding/receipt originales intactos. ID y reason copiados iguales a unknown_job se rechazan y sus cuatro campos auditados muestran marcadores, sin binding. Lectores no reescriben historia; control/capacidad/transiciones/contador/supervisor sin efectos. En d5, el primer public nuevo (antes) o exacto tras declaración (después) pierde api; también decision/error/effects. El oráculo de literal falla por inspección. |
| r4_a01_effective_control_preserves_committed_and_historical_replay | Origen vacío noGit, submit enqueue-only declara committed y fleet-job-api@1; lector sin esas variables. Drain efectivo confirma control drenado y receipt/status/decision committed; reopen separado abre. Replay del drain conserva historia y control actual abierto. R6 separa inventario íntegro de lectores y comparación durable del replay descrita abajo; dos operaciones/dos eventos committed. Reason copiado committed se rechaza y redacta en evento/request, conservando códigos/efectos; no drena ni despacha el holder. En d5 ya la respuesta drain pierde api/status/decision; sin la colisión api también fallan los asserts committed. No se mide runtime r6. |

Dos métodos, **2 invocaciones subTest nuevas** por expansión del primer método;
el segundo no usa subTest. Se conservan62 métodos, cuatro adiciones r3/29 subTest,
cinco r2/27 subTest y las34 filas26/6/2, más A-W1/B-W1/B-W2. **0 ejecutados**,
incluido d5. Una historia cuyo productor ya borró un literal no se repara aquí;
el caso posterior declara credenciales tras producir historia auténtica intacta.

Corrección R4-A01/F1 en [SEN-3820-r5.md](../SEN-3820-r5.md): evento y recibo tienen
capturas de tiempo independientes. El primer método valida por separado sus tiempos
UTC con microsegundos en esta fixture y conserva cada valor desde su propio origen,
antes/después de declarar credenciales, en replay/status y todos los recorridos de
páginas pertinentes. No exige igualdad, orden ni delta entre relojes. Mantiene las
igualdades de destino/decisión/acción/actor/motivo/revisión, enlace audit_seq y todos
los asserts posteriores de privacidad, binding y ausencia de efectos. Los64 métodos
y los2 subTest r4 se conservan, **0 ejecutados**; N2 de credenciales cortas sigue diferido.

## Prerrequisitos externos y registro futuro

No se han creado ni comprobado recursos de ejecución durante esta entrega.
La futura corrida debe conservar entorno/certificación/head e intérprete exactos.
Ausencia de un prerrequisito falla explícitamente; no hay skips que sumen cobertura.

| Condición | Variables/entrada futura | Parte no acreditada |
| --- | --- | --- |
| Directorio de corrida | `FLEET_TEST_RUN_BASE`, físico, owner actual0700 | Sin él ninguna fixture arranca. No usar residuos3821 ni un temporal compartido. |
| Cuota pao-cs propia | `FLEET_TEST_QUOTA_BASE=/Volumes/pao-cs/fleet-tests`, `FLEET_TEST_ISOLATED_QUOTA=1`, `FLEET_TEST_QUOTA_FILL_LIMIT` positivo ≤536870912 | Revalidación futura disk3s7/UUID F4FF2E7E-751F-48B3-BA0D-F5F2656C23BC/Case-sensitive APFS/Owners Enabled/cuota exacta512MiB/cosmonaut0700. Sin espacio previo o con cambio de metadatos ajenos: BLOCKED. La estabilidad no prueba ausencia de uso ajeno. R6 sólo setup; falta discriminante de frontera aprobado, no admite ni llena. |
| Esquemas auténticos | `FLEET_TEST_SCHEMA2_CLI`, `FLEET_TEST_SCHEMA3_CLI`, copias públicas completas de CLI reales | No se siembran tablas/versiones. Inventarios activos sólo con comando en barrera sin escritor antiguo avanzando. |
| Retención edad | `FLEET_TEST_AGED_AUTHORITY`, `FLEET_TEST_AGED_KEY`, autoridad propia e historial auténtico>7d | Sólo consulta; no registra admisión ni limpia/modifica ese estado externo. Falta edad incierta si el historial sólo es finished. No existe clock flag del producto. |
| Negativos OS O03 | Otra cuenta/ejecutor externo y entorno que deniegue realmente identidad nativa | No implementados como sustituto env. Sin ellos los dos negativos permanecen bloqueados. No crear cuentas/permisos globales. |
| Ventanas internas | Control causal externo real, aún no identificado en CLI | Claim/vínculo/R/compuerta/Popen/fsync/commit exactos y B-W1 pre-spawn siguen pendientes; campañas registran sólo lo observado. |

`cleanup` registra todas las admisiones antes de submit; sin stdout usa K. Rechazo
candidato confirmado no se confunde con ausencia de asociación histórica.
Incertidumbre/atribución faltante no autoriza borrar. Se abren gates de fixtures y
se recogen clientes; se exige ausencia observable de supervisores/grupos publicados.
Snapshots, campañas y contadores se guardan con fsync fuera del root eventualmente
retirado. Antes de retirar una fixture completa se conservan también bytes de
manifiestos/payloads públicos, cotejando hash e identidad estable sin atime; se
sincroniza el padre de evidencia. Un error impide retirar originales.
Estados con completion incomplete se retienen, incluso released con evidencia
posteriormente inválida. Una cuota propia fuera del root común se conserva y figura
en owned_bases. C23 conserva snapshots públicos antiguos en el informe; el historial
aged ajeno a la fixture es sólo lectura.

## Conservación de las16 pruebas base

Se mantienen aceptación/identidad; client; argv/cwd/stdin/logs; exit/señal; FIFO y
concurrencia; entorno/PATH por emisor; descendiente en grupo; atribución antes de
limpieza; estado público rechazado sin cambios; timeout/SIGINT del lector;
aislamiento de señales; supervisor perdido; fallo de lanzamiento; dos estados;
errores/log perdido/selección; default/reservados.

Cambios intencionales versionados: JSON en observation y errores tipados; cwd ahora
materializado; fallo de supervisor conserva accepted histórico (método renombrado);
finished/publication pending da3 y helper base sólo reobserva esa ventana; run base
obligatorio, watchdog30s y conservación de evidencia/incompletos. No se elimina un
caso para acomodar un resultado del runtime. R7 no se ha ejecutado; los resultados
históricos de c63 se registran abajo sin reclasificarlos.

## Aclaración r6 y correcciones r7 respecto de c63

El bloqueo inicial del autor se conserva como antecedente. PAO-CLARIFICATION-1
autoriza implementar los tres arreglos y el setup; no autoriza inventar una
observación causal externa. Si se identifica una propuesta admisible, PAO decide
antes de implementarla. No hay nueva observación implementada ni cuota ejecutada.

Lost_supervisor usa una variante local de BARRIER que omite sólo el print; done
es independiente del pipe cuyo lector murió. Sigue SIGKILL al supervisor, wait3,
slot incierto retenido, segundo queued/sin ready y transición única queued. Los
otros usuarios de barrier(), sus variantes de descendientes y entorno/PATH y los
oráculos stdout=ok siguen usando el BARRIER original. No se captura ni acepta
genéricamente un fallo del comando y no se acepta done ausente.

En ambos r4_a01, el helper r4_replay_boundary conserva inventarios completos
antes/después de los lectores a cada lado del replay. Inventory global sigue
incluyendo SHM/WAL, modo/tamaño/mtime/ctime/hash. Sustituye la igualdad física
cruzando el escritor por igualdad contractual CLI: api/operación/autoridad/uid,
revisión, control, operaciones y auditoría completas, seq, recibos, los dos
cursores y fin. Pagina ambos streams limit1 con avance y fin explícitos. Compara
además todas las respuestas status de los holders, incluido el de declaración
posterior, excepto el único observed_at de observación. Esto cubre identidad,
admisión/input, reserva/capacidad, dispatch, transiciones y revisión sin SQL.
Mantiene before/after/control, request/binding exactos, privacidad tras declarar,
literales y las dos historias temporales independientes r5; tampoco elimina los
asserts posteriores de secreto en motivo/ID, conflicto pese a igual redacción,
contador vacío y ausencia de supervisor. No infiere mutación lógica desde SHM.

El setup de cuota inventaría sólo metadatos inmediatos del base y su raíz propia
recursiva. Identifica cada raíz por creación exclusiva/dev/ino/uid0700, sin
atribuir por prefijo; state/project/home/fixtures se preparan dentro. R7 activa
retención administrativa antes del primer quota_record/preflight/mkdtemp: cleanup
conserva self.root y journal original aunque no haya raíz de cuota ni admisiones
o falle esa primera escritura. El snapshot exterior no sustituye los originales.
El registro separa fase real y objetivo, primaria/secundarios, raíz creada y
atribución confirmada. Cada inventario es complete sólo tras enumeración íntegra,
incomplete con entradas parciales y error, o not_performed con motivo. Finally
intenta inventarios permitidos y registro final aun en fallos de configuración,
certificado, espacio, mkdtemp o metadatos. No recorre una raíz sin atribución ni
un base cuya identidad no se confirmó. Conserva rc/stderr de diskutil o error de
consulta/timeout sin publicar el entorno privado. Errores de inventario, persistencia
y cierre quedan explícitos y no sustituyen la misma primaria; sin primaria previa
se propaga el primer fallo real. Journal fuera de cuota: persistence sólo pasa a
synced en memoria tras escritura/fsync de archivo y directorio/cierres completos;
si falla permanece unconfirmed, sin promesa de durabilidad. Se conservan los
originales disponibles incluso si no se puede persistir el diagnóstico final.
No limpia históricos ni genera relleno. Los dos subTest O06
permiten informar ambas fases aunque una precondición falle; siguen64 métodos,
con2 invocaciones subTest condicionales explícitas en O06,0 métodos nuevos.

PASS de cuota requeriría ENOSPC/EDQUOT real en fase objetivo probada, no sólo
llenado, apertura fallida o errno genérico: C20 con ack terminal propio y O06 con
commit de capacidad/auditoría correlacionados. Barreras deben prepararse antes de
llenar y su propio fallo nunca acredita producto. Sólo se podría restituir el
relleno propio atribuido en finally con registro; raíces/estado/evidencia quedan.
Esa experimentación permanece pendiente de una decisión posterior sobre el
discriminante; r6 falla BLOCKED después de setup y jamás produce skip/PASS.

Evidencia histórica c63, run20261026-143952-e9b9eb:3.9=56pass/6fail/2error,
3.14=58pass/4fail/2error,0skip; C20/O06/C23 son3 BLOCKED por stack. C07/C08
timeout5s, C08 además settle10s y63/64 reportes públicos; cierre posterior por
ausencia20:50:26Zrc0 no cambia esas pruebas ni rellena los reportes. Lost_supervisor
no llegó a done; los r4_a01 fallaron inventario sólo3.9. El mecanismo de señales y
BrokenPipe sigue siendo hipótesis histórica delimitada, sin getsignal/EPIPE
medidos de esos clientes. La inclusión/efectos físicos SHM y ventana escritora
sí fueron constatados en fuentes públicas. Son discriminantes históricos de los
tres arreglos; C20/O06 no tuvieron baseline FAIL causal porque no pasaron setup.
R6 conserva64 métodos,0 ejecutados, sin afirmar PASS ni aceptación de plataforma.
C23 permanece BLOCKED; A6/C21 positivo/O10/ventanas/OS mantienen sus límites.

R7 conserva los64 nombres y MRO,0 métodos nuevos/skips y0 ejecutados. Sus tres
discriminantes contra3b son estáticos: fallo antes de quota_roots ya no permite
rmtree del journal; C08 fotografía después de running durable, evitando atribuir
a SIGHUP una transición legítima starting→running; C07 rechaza la respuesta
exterior unknown/unknown que antes satisfacía130/client_interrupted. No hay
FAIL/PASS medidos de esos recorridos. Lost_supervisor y replay r6 quedan intactos;
B-N1/B-N2 y nits anteriores diferidos. Ningún observador causal nuevo ni frontera
C20/O06/C23 cerrada por esta revisión.
