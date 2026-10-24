# Cobertura del candidato Sol, ronda 3

Autoridad: `f6dea9a7afd182dff9742d52784dc4e98aeb451f`, PR #17, contenido idéntico a `a1f00fa`. Esta tabla distingue
implementación de sombra, simulación con harness y aceptación futura. No acredita
activación, integración de consumidores, aislamiento hostil ni corte real del runner.
Rutas relativas a `fleet/hostgate/`.

| Línea S del sketch | Archivo | Prueba / límite |
| --- | --- | --- |
| 02 | `__init__.py`, `cli.py`, `README.md` | `CLI.test_owner_payload_cannot_construct_auth`, `Contract.test_desk_no_transport_even_degraded`; Python 3.9 stdlib. |
| 21 | `admission.py`, `protocol.py`, `store.py` | `Contract.test_shadow_only_and_future_interfaces_closed`; ningún permiso efectivo. |
| 37 | `model.py:expire_pending`, `admission.py:_persist_effective` | `Storage.test_queries_never_mutate_sqlite_or_wal`, `test_replay_persists_expiry_without_renewing_or_regranting`, `Contract.test_jale_real_helpers_bound_to_three_preparations`; sin timer. |
| 56 | `admission.py:Hostgate`, `cli.py` | Casos de `Contract`, `CLI`; API sólo simulación, IPC futuro. |
| 72 | `cli.py`, `tests/support/world.py` | `CLI.test_owner_payload_cannot_construct_auth`, `CLI.test_public_cli_harness_and_strict_json`; endpoint sin auth rechaza. |
| 74 | `protocol.py:reply`, `admission.py:_run` | `Contract.test_shadow_only_and_future_interfaces_closed`, `Contract.test_replay_authentication_conflict_and_historical_state`; key dentro de `simulationKey`. |
| 90 | Estructura del paquete | Inspección `git diff --check`; sin módulos productivos F. |
| 91 | `README.md`, `__init__.py`, `__main__.py`, `cli.py`, `protocol.py`, `model.py` | `CLI`, `Contract`, `Storage`. |
| 92 | `admission.py`, `store.py`, `identity.py`, `scope.py`, `owner_record.py` | `Contract`, `Storage`, `Boundaries`. |
| 93 | `salud/__init__.py`, `salud/lector_desk.py`, `salud/lector_mini.py` | 112 casos originales desk adaptados a unittest, `MiniReader`; ejecutados en Mini, ver estado r2 al final. |
| 94 | `fixtures/jale-2254z*.json`, `fixtures/health` | Jale y lectores consumen fixtures; eliminados los tres JSON sin caller; no equivalen a aceptación nativa. |
| 95 | `tests/test_cli.py`, `test_store.py`, `test_contract.py`, `support/` | Suite unittest prevista por run.sh; pruebas escritas de clientes de varios procesos y SQLite real, ejecutadas en Mini, ver estado r2 al final. |
| 96 | `identity.py`, `scope.py`, `salud/lector_mini.py` | `Boundaries`, `Contract.test_scope_and_cleanup_all_or_nothing`, `MiniReader`; perfil/cierre real no acreditado. |

Las etiquetas S explícitas están en 02, 21, 37, 56, 72, 74 y 90; 91–96 desarrollan
la estructura S. Se añade cobertura de las reglas ejecutables sin etiqueta:

| Líneas | Archivo | Prueba / motivo de no cobertura real |
| --- | --- | --- |
| 01, 04–06, 08 | `README.md`, `model.py`, `store.py` | Configuración administrativa inyectada; `Storage.test_configuration_symlink_and_enforcing_rejected`. Persistencia/localidad del filesystem productivo no acreditada. |
| 07, 19 | `store.py` | `Storage.test_missing_corrupt_wrong_store_never_recreated`, `test_busy`, `test_commit_cuts_and_response_lost`. |
| 09–10, 52 | `identity.py` | `Boundaries.test_darwin_structure_and_live_identity_read_only`, `test_linux_exact_stat_parentheses`; lectura real Darwin acotada prevista y fixture proc Linux (`test_linux_proc_fixture_exact_token_reuse_zombie_and_missing`); ejecutadas en Mini, ver estado r2 al final. |
| 11–12 | `protocol.py`, `admission.py` | `CLI.test_demand_and_nul_validation`, `Contract.test_minimum_and_unknown_resource`, `test_budget_dimensions_and_exclusives`. |
| 13–14, 22–27 | `model.py`, `admission.py` | `Contract.test_jale_real_helpers_bound_to_three_preparations`, `test_channel_eof_withdraws_without_reservation`, `test_commit_revalidation_pid_anchor_channel_heartbeat`. |
| 15–18, 28–29 | `admission.py`, `scope.py` | `Contract.test_death_after_revalidation_retains_commit`, `test_budget_dimensions_and_exclusives`, `test_old_generation_never_alters_new`. |
| 20, 84–85 | `owner_record.py`, `README.md` | `Boundaries.test_owner_record_partial_and_root_terminal_protect`, `test_owner_fsync_failure_preserves_protection`. No cubierta integración U1: consumidor actual aún no entiende Fleet. |
| 30, 33, 40–42 | `scope.py`, `README.md` | `authorize_creation`/`record_started` fallan cerrados. No cubierto runtime de supervisor, permiso, publicación previa a efectos o reconciliación: F. |
| 31–32, 34–36 | `model.py`, `admission.py`, `scope.py` | `Contract.test_ttl_owner_lost_boot_and_heartbeat_cannot_release`, `test_scope_and_cleanup_all_or_nothing`, `Storage.test_queries_never_mutate_sqlite_or_wal`. |
| 43–50 | `scope.py`, `README.md` | Cierre positivo sintético fenced y casos negativos; `Contract.test_windows_fixture_only_positive_evidence_fenced`. No cubierta contención/launcher/cierre nativo real: F. |
| 51 | `README.md`, código sin control de procesos | Inspección y helpers propios con autoexit; no SIGSTOP ni terminación de procesos. |
| 54–55 | `admission.py`, `store.py`, `cli.py` | `Contract.test_replay_authentication_conflict_and_historical_state`, `Storage.test_commit_cuts_and_response_lost`; autenticación real espera F. |
| 57–62, 65–71 | `admission.py`, `model.py`, `salud/`, `cli.py`, `protocol.py` | `CLI`, `Contract`, `MiniReader` y 112 casos desk. |
| 73, 75–78 | `protocol.py`, `cli.py`, `admission.py` | `CLI.test_cli_all_commands_and_exit_mapping`, `Contract.test_replay_authentication_conflict_and_historical_state`. |
| 79–81 | `salud/`, `admission.py` | Lectores y `Contract.test_desk_no_transport_even_degraded`; transporte invocado cero veces. |
| 83 | `README.md` | No cubierta ordenación Fleet: pertenece al consumidor; hostgate muestra degradación y barreras sin admitirla. |
| 86–89 | `README.md`, `model.py`, fixtures Jale | Adaptador modelado, slots retenidos, boot drena. No cubierto corte/rechazo real de runners antiguos: activación F. |
| 98–100 | `tests/test_store.py`, `test_contract.py`, `test_cli.py` | Pruebas escritas de concurrencia por API y procesos CLI con presupuesto compartido; exclusivos, límites, replay, conflictos y generación vieja. |
| 101 | `tests/test_store.py`, `test_contract.py` | Cortes modelados antes de escritura/commit y ACK perdido; muerte postrevalidación. No cubiertos permiso/spawn reales: F. |
| 102–104 | `fixtures/jale-2254z*.json`, `tests/support/helper.py`, `test_contract.py` | Dos reproducciones modeladas, helpers reales EOF/legado acotado y variante que pudo crear. Adaptador run.sh real no cubierto: F. |
| 105 | `test_contract.py`, `test_identity_scope.py` | PID exacto/reutilización, EPERM modelado, latido, reloj/boot físico/ejecutor. |
| 106 | `test_store.py`, `test_identity_scope.py` | BD ausente/corrupta/storeId, busy, cortes/commit incierto; ENOSPC/fsync inyectado en publicación. No se llena disco real. |
| 107–108 | `test_contract.py` | Cierre/limpieza conservadores y Windows positivo sólo modelado. |
| 109–110 | `test_lector_desk.py`, `test_lector_mini.py`, `test_contract.py` | Corpus, Mini, falta de testigo y cero transporte. |
| 111–112 | `test_store.py`, `test_contract.py`, `test_identity_scope.py` | Consultas sin mutación, drenado por boot, pausa/slots/registro incompleto con protección. |
| 113 | Guion/evidencia de comprador | Sólo Mini por run.sh con turno; sin flags de producción. |
| 03, 38–39, 53, 63–64, 82, 97, 114–115 | `scope.py`, `cli.py`, `README.md` | F: no servicio/timer/IPC/supervisor/transporte/permiso productivos; interfaces disponibles rechazan. Sin activación por fixtures. |

| Corrección §5 | Archivo | Prueba / límite |
| --- | --- | --- |
| 1. Autoridad vigente | `README.md` | Inspección del merge contractual `f6dea9a7` / PR #17; Mini aceptada, no se pide nueva aceptación. |
| 2. Wire y generación | `protocol.py`, `admission.py`, `store.py` | `Contract.test_old_generation_never_alters_new`, `test_replay_authentication_conflict_and_historical_state`, `Storage.test_concurrent_budget_and_parallel_replay`. |
| 3. Preparación sin reserva tentativa | `model.py`, `admission.py` | `Contract.test_jale_real_helpers_bound_to_three_preparations`, `Storage.test_missing_corrupt_wrong_store_never_recreated`. |
| 4. Pausa y cierre conjunto | `scope.py`, `admission.py` | `Contract.test_scope_and_cleanup_all_or_nothing`, `test_positive_closure_evidence_is_durable`, `test_malformed_closure_provider_fails_closed`; todo o nada. |
| 5. Unix perfil + ámbito + linaje | `scope.py`, `README.md` | `Contract.test_scope_and_cleanup_all_or_nothing`; perfiles reales rechazados, garantía real pendiente F. |
| 6. Autenticación realizable | `model.py`, `cli.py`, `README.md` | `CLI.test_owner_payload_cannot_construct_auth`, `Contract.test_replay_authentication_conflict_and_historical_state`; transporte/peer real F no cubierto. |
| 7. Salud/transporte cerrado | `salud/`, `admission.py` | Lectores, `Contract.test_desk_no_transport_even_degraded`, `test_unknown_health_reason_is_barrier`; sin degradación admisible inventada. |
| 8. Motivos estables | `protocol.py`, `admission.py` | `Contract.test_budget_dimensions_and_exclusives`, `CLI.test_cli_all_commands_and_exit_mapping`; catálogo cerrado. |
| 9. Registro U1 | `owner_record.py`, `README.md` | `Boundaries.test_owner_record_partial_and_root_terminal_protect`, `Contract.test_supervisor_lost_with_descendant_alive_retains_all`; integración U1 no cubierta, requiere acuerdo versionado. |
| 10. Frontera real de migración | `README.md`, fixtures Jale | No cubierta: no se permite alterar/adaptar run.sh en este PR de sombra. Se documenta el corte antes de abrir Fleet. |
| 11. Jale, cinco condiciones/dos fixtures | `fixtures/jale-2254z*.json`, `test_contract.py`, `tests/support/helper.py` | `Contract.test_jale_real_helpers_bound_to_three_preparations`, `Contract.test_jale_independent_causes_and_possible_creation`; reproducción, no captura original. |
| 12. Tamaño y pruebas honestas | `model.py`, `store.py`, `cli.py`, `README.md` | `Storage.test_queries_never_mutate_sqlite_or_wal`, guion Mini y recibos; sin daemon/polling ni afirmaciones de IPC real. |


Registro histórico al cierre inicial de r2: dos corridas remotas con turno, ambas sobre árbol
limpio. `72a6d10` y `731d866` descubrieron 174 pruebas cada una; cada corrida tuvo
173 pruebas correctas y un fallo, sin errores ni omisiones. El primer intento de
lanzador `43d94850ecba47a88839709894495440` terminó con salida 130 esperando turno,
sin run_id ni pruebas. No hubo pruebas en MacBook.

| Familia / pruebas | Estado en Mini |
| --- | --- |
| CLI, 6 | Ejecutadas, PASS en las dos corridas |
| Contrato, 28 | Ejecutadas; Jale falló en `72a6d10` y pasó sus cuatro escenarios en `731d866`; las otras 27 pasaron en ambas |
| Identidad/ámbito/U1, 8 | Ejecutadas, PASS en ambas |
| Desk, 113 (112 originales + contraste) | Ejecutadas, PASS en ambas |
| Mini, 4 | Ejecutadas, PASS en ambas |
| Almacén, 15 | Ejecutadas; todas PASS en `72a6d10`; 14 PASS en `731d866` y fallo de orden en `test_two_executors_shared_world_budget_and_observation_changes` |
| Corrección `45b0e39` de esa aserción | No ejecutada en esas dos corridas; ejecutada después en `dad5ef8` (actualización fechada abajo). Identifica por executorId, sin depender del orden de commits |
| Inspección documental y sintaxis AST | Inspección local, no prueba runtime |

B-G1, B-G2 y B-G3 tienen pruebas escritas antes del código corrector. Fallaron sin
las correcciones en el commit `3f8ce69` reconstruido en Mini y pasaron en ambos SHA
corregidos. Ese rojo fue retrospectivo, no anterior a los commits correctores.
Para los otros cambios no se ejecutó rojo previo; Jale y orden concurrente sí tienen
fallos observados antes de sus correcciones posteriores. Al redactar ese registro aún no había suite verde sobre el head final; la tercera
corrida de la actualización fechada abajo supersede ese estado.

| Cambio obligatorio §6 / error | Archivos | Pruebas específicas añadidas o reforzadas |
| --- | --- | --- |
| 1 / B-G1, injerto 1 | `model.py`, `scope.py`, `admission.py`, `tests/support/world.py`, `tests/test_contract.py` | `test_b_g1_windows_proof_cannot_release_unix`, `test_scope_and_cleanup_all_or_nothing`, `test_windows_fixture_only_positive_evidence_fenced` |
| 2 / B-G2, injerto 4 | `admission.py`, `store.py`, `tests/test_contract.py`, `tests/test_cli.py` | `test_b_g2_replay_requires_exact_peer_and_channel`, `test_reserve_replay_and_query_role_same_peer_are_not_authority`, `test_heartbeat_sequence_replay_and_different_live_peer` |
| 3 / B-G3 | `admission.py`, `tests/test_contract.py` | `test_b_g3_revalidate_after_health_io`, `test_death_after_revalidation_retains_commit` |
| 4 / Jale, injerto 2 | `tests/support/helper.py`, `tests/support/world.py`, `tests/test_contract.py` | `test_jale_real_helpers_bound_to_three_preparations` (dos fixtures, cuatro escenarios), `test_jale_independent_causes_and_possible_creation` |
| 5 / B-M2, B-M3 | `scope.py`, `model.py`, `store.py`, `tests/test_cli.py`, `tests/test_store.py` | `test_malformed_provider_keeps_json_envelope_and_all_resources`, `test_valid_checksum_does_not_authorize_invalid_rows`, `test_sqlite_full_io_and_secondary_cleanup_preserve_uncertainty` |
| 6 / B-M1, B-M6 | `admission.py`, `tests/test_store.py` | `test_digest_conflict_persists_expiry_without_changing_original`, `test_state_reads_health_exactly_once`; coste de quick_check/reescritura sin medir |
| 7 / B-M4, B-M7, injerto 5 | `owner_record.py`, `tests/test_identity_scope.py`, `tests/test_contract.py` | `test_owner_root_identity_must_be_exact_and_separate`, `test_directory_fsync_after_replace_keeps_published_protection`, `test_owner_fsync_failure_preserves_protection`, `test_explicit_dudosa_retains_after_publication_failure`, `test_owner_rejects_production_symlink_and_stale_sequence` |
| 8 / B-M5, injertos 3 y 4 | `model.py`, `admission.py`, `store.py`, `tests/support/world.py`, `client.py`, `tests/test_store.py` | `test_two_executors_shared_world_budget_and_observation_changes`, `test_trusted_initial_drain_cannot_be_reopened_by_query`, concurrencia/replay con retry busy acotado |
| 9 / trazabilidad | `README.md`, `COBERTURA.md` | Inspección documental. Aceptación Mini ya existente; límites F conservados |
| 10 / evidencia | Guion y evidencia externa autorizada | Seis familias por unittest; estado según recibos. No cubierta activación |
| Desktop d8fecdc7 | `tests/test_lector_desk.py` | `test_observacion_powershell_contraste_d8fecdc7`: ausente, complete0, completewarn, partialunknown, unavailable, diagnosticutc; sano y errores superiores conservados |

No se acredita la liberación por perfil real Unix ni launcher Windows; sólo tipos,
fencing, retención y transiciones sintéticas. La inyección SQLite no es una prueba
VFS del fsync interno del ledger. Las pruebas de publicación no integran el consumidor
U1. El coste con historial largo sigue sin medir. Los tres fixtures JSON de configuración/identidad/launcher sin caller se eliminaron
en r3; no acreditaban ejecución nativa.


Recibos de las dos corridas: `36ec0d9c79bc4457824e988d186493e8` y
`41feb9cb75f0489c8ab00e2d1f76bc85` en la evidencia del encargo. SHA completos,
stdout/stderr y códigos están conservados. Último cambio funcional de producción
probado: `731d866f1c3461fd5bac00a9013c95abe077742b`; después sólo cambian una
aserción de prueba y documentación. Ese era el estado al cerrar las dos primeras corridas; véase actualización posterior.


Actualización fechada 2026-10-23 (America/Mexico_City), I-1 / A-M3: se conservan
las dos corridas fallidas anteriores y se registra la tercera, run
`20261023-192300-c75aef`, SHA `dad5ef88158daffe76a2a47566fec4924502da5d`.
Resultado: 174 pruebas / 0 fallos / 0 errores / 0 omitidas; CLI 6, contrato 28,
identidad/ámbito/U1 8, desk 113, Mini 4 y almacén 15. Las tres regresiones
B-G1/B-G2/B-G3 dieron rojo sobre `3f8ce69` en la misma corrida y verde sobre
`dad5ef8`. Evidencia:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/9df0741daa4d4a629ac47bf2c4b6be5a/lanzador.log`.
Suite verde sobre ese SHA no equivale a aceptación técnica; las dos reviews
posteriores y el encargo r3 mandan sobre el trabajo aún pendiente.

| Punto r3 | Prueba / límite |
| --- | --- |
| 1, A-I1 | `test_a_i1_foreign_peer_cannot_consume_prepared_operation`, `test_a_i1_foreign_mutations_leave_ids_available_to_owner`; replay B-G2 y rechazo durable conservados. |
| 2, A-M1 | `test_valid_checksum_does_not_authorize_invalid_rows`, `MiniReader.test_malformed_fields_never_succeed_or_raise`, `test_huge_volume_and_clock_are_invalid`, `test_owner_huge_started_is_invalid_and_read_protects`, `test_desk_huge_integer_is_invalid_at_adapter_boundary`. Lector desk entonces intacto; A-M1 resuelto por adopción r4. |
| 3, M-2 | `test_boot_change_after_revalidation_keeps_ledger_usable`. |
| 4, M-1/A-M2 | `test_observation_skips_terminal_history_without_erasing_it`; siguen `quick_check` y reescritura completa, coste pendiente de medir. |
| 5, M-7 | `test_internal_provider_errors_are_not_request_errors`, `test_internal_query_failure_and_interrupt_keep_envelope`; incluye exit 130. |
| 6, M-8 | `test_state_wire_maps_granted_to_simulated`; sketch 25 interno, `simulated` público. |
| 7, M-4 | `test_closure_fencing_fields_and_never_created_each_fail_alone`; digest recalculado salvo el negativo específico de digest. |
| 8, M-5 | `test_pending_deadline_expires_before_heartbeat`; TTL de latido mayor que el de preparación. |
| 9, M-6 | `test_desk_no_transport_even_degraded` aísla DefaultShell con testigo válido. Renombrados `test_absent_reused_and_unverifiable_peer_is_unauthorized` y `test_explicit_dudosa_retains_after_publication_failure` según su alcance real. |
| 10, M-3/A-M4 | `test_observacion_powershell_contraste_d8fecdc7`: seis variantes y dos aserciones; errores agregados fieles a 472–498, corpus de 112 intacto. |
| 11, M-12 | `test_helper_json_publication_never_exposes_partial_target`, Jale publica ready/retries por replace atómico. Los padres salen con `os._exit`; no se simula muerte por señal. |
| 12, M-11 | Código/fixtures huérfanos eliminados; `test_owner_boot_allows_separator_but_ids_do_not`; sin reformateo global. |
| 13, M-9/M-10 | README: operator sin autoridad sobre otro supervisor; recuperación explícita F (SEN-3834/3835/3836). ProcessTable iguala boots y debe corregirse antes de integrar WSL. |
| 14, I-1/A-M3 | Registro fechado de tres corridas históricas y corrida r3 con SHA/recibo abajo. |
| Reconexión | F: no hay IPC/canal de reconexión autenticado ni supervisor productivo; el replay local no demuestra reconexión. |

Actualización de ejecución r3, 2026-10-23 (America/Mexico_City): run
`20261023-204639-a55097`, SHA probado `147ff527d04b6f622bce940ea27ed999520443e7`.
Árbol limpio y congelado durante espera/ejecución, Mini con turno y Python 3.9.6.
Una corrida, salida 0; no se usó la segunda. No hubo pruebas ni imports del candidato
en MacBook; sólo inspección AST de sintaxis 3.9 y diff.

| Familia | Descubiertas / ejecutadas | Resultado |
| --- | --- | --- |
| CLI | 9 | PASS |
| Contrato | 35 | PASS |
| Identidad/ámbito/U1 | 11 | PASS |
| Desk | 113 (112 originales + contraste) | PASS |
| Mini | 5 | PASS |
| Almacén | 15 | PASS |
| Total | 188 | 0 fallos / 0 errores / 0 omitidas |

A-I1: las dos pruebas nuevas dieron rojo sobre `dad5ef8` en esta misma corrida,
2 fallos y 0 errores de entorno: el dueño recibía exit 5 en lugar de 0 tanto en
reserva preparada como en latido. Dieron verde en el SHA probado; la secuencia
completa corregida incluye soltar con el mismo ID que el peer rechazado. B-G2 y
el rechazo durable autorizado también dieron verde. Fue rojo retrospectivo:
pruebas escritas antes del código, ejecución posterior en Mini con snapshot temporal.
El guion conserva las seis familias y exige exactamente 113 casos desk.

Evidencia completa, sin recortes:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/495ef06ee60541bd8626124333b1b7f4/remoto.log`.
En el mismo directorio: `lanzador.log`, `script.sh`, `recibo.json` y `terminal.json`;
recibo completo, resultado y salida del lanzador 0, turno liberado. Los procesos
propios del lanzador terminaron; los helpers propios concluyeron en la suite.
El commit posterior cambia sólo README/COBERTURA, sin nueva ejecución. Esto acredita
suite verde sobre el SHA citado; la aceptación técnica de Fleet sigue pendiente,
al igual que las interfaces F. A-M1 se resuelve por adopción r4, registrada abajo.


Adopción A-M1, 2026-10-23 (America/Mexico_City): copia byte a byte de
`/Users/felixddhs/.claude/encargos/desktop-carga/lector-numero-am1/artifact/lector_desk.py`,
Desktop `d21312b1`, empaquetado en `ca44846d`. Blob verificado antes del commit:
`1ef0465d847836129dc155a80bf34fac8e169a4a`. Desktop acreditó en Mini los runs
`20261023-205755-d82822` (112/112 en base y corregido; regresiones 22 fallos en base
y 36/36 en corregido) y `20261023-210507-aedc03` (113/113 del corpus hostgate en ambos).
El corpus `test_lector_desk.py`, `support/cases.py` y `fixtures/health/desk.json`
conserva sus blobs. `test_lector_desk_adoptado.py` añade dos casos de JSON ±10**400
por el adaptador, con control sano, degradación y rechazo; se mantiene la captura
de desborde de `admission.py`. No queda pendiente A-M1 para Desktop. La evidencia
de ejecución de esta adopción se registra con la corrida r4.
