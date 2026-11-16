#!/usr/bin/python3 -I
"""U3.6: una mutación por regla de seguridad de reordenar, en copias temporales.

Cada mutación debe hacer fallar su prueba de `test_reorder.py` por aserción
(FAIL, nunca ERROR). Python 3.9 y 3.12: `python3 mutate_reorder.py`.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
C, E = 'control.py', 'editor_prioridad.py'
# Regla, archivo, fragmento original, mutación, prueba que debe rechazarla.
MUTATIONS = [
    ('pausa --wait antes de editar', C, "            self._rpc('pause', '--wait', '-g', group)\n            snapshot = self.pueue.status()\n            if snapshot['groups'][group]['status'] != 'Paused':\n                raise",
     "            snapshot = self.pueue.status()\n            if False:\n                raise", 'test_drag_five_to_two_applies_receipt_and_reopens_through_admission'),
    ('razón reorder en la misma transacción que la intención', C, "            state['reasons'][group] = sorted(set(state['reasons'].get(group, [])) | {'reorder'})\n            self.store(db, state)\n            db.execute('INSERT INTO host_actions",
     "            db.execute('INSERT INTO host_actions", 'test_crash_at_each_frontier_is_settled_by_the_single_recoverer'),
    ('comparación exacta tras la pausa', C, "            if ({t['id']: [task_identity(t), t['priority']] for t in rows}\n                    != {i: [r['identities'][str(i)], r['before'][str(i)]] for i in r['plan']}):",
     '            if False:', 'test_started_added_or_cancelled_before_comparison_edits_nothing'),
    ('comparación previa contra lo servido', C, "if e['generation'] != generation or e['queue_etag'] != etag or e['queue'] != served:",
     'if False:', 'test_stale_expected_queue_is_conflict_without_pause'),
    ('no escribe valores ya vigentes', C, "writes = [i for i in plan if target[str(i)] != before[str(i)]]",
     'writes = list(plan)', 'test_unchanged_targets_are_not_written'),
    ('presupuesto rechaza antes de pausar', C, 'if estimate > REORDER_PAUSE_SECONDS:',
     'if False:', 'test_plan_over_pause_budget_is_rejected_before_pausing'),
    ('plazo de pausa durante la acción', C, "if now + cost * (len(r['writes']) - done) > deadline:",
     'if False:', 'test_budget_expiry_mid_action_aborts_and_restores'),
    ('el plazo proyecta con el ritmo observado', C, '(now - first) / done if done else 0)',
     '0)', 'test_slow_edits_abort_early_with_little_to_roll_back'),
    ('cada RPC con plazo de 1 s', C, 'previous, self.pueue.timeout = self.pueue.timeout, REORDER_RPC_SECONDS',
     'previous, self.pueue.timeout = self.pueue.timeout, None', 'test_every_rpc_has_one_second_deadline_and_pause_precedes_any_edit'),
    ('alias en la primera palabra es edit_unsafe', C, 'if words and words[0] in aliases:',
     'if False:', 'test_edit_unsafe_is_rejected_before_any_pause'),
    ('comando expandido es edit_unsafe', C, "    if task['command'] != task['original_command']:\n        return True",
     '    if False:\n        return True', 'test_edit_unsafe_is_rejected_before_any_pause'),
    ('sólo modo de edición TOML', C, "if settings['edit_mode'] not in (None, 'toml'):",
     'if False:', 'test_edit_unsafe_is_rejected_before_any_pause'),
    ('tareas apartadas bloquean', C, "if any(task_state(t)[0] in ('Locked', 'Stashed', 'Paused')",
     'if any(task_state(t)[0] in ()', 'test_held_tasks_block_reorder'),
    ('grupo pausado sin motivo bloquea', C, "if snapshot['groups'][group]['status'] != 'Running' or reasons:",
     'if reasons:', 'test_paused_group_is_rejected_with_its_reason'),
    ('tope de 64 pendientes', C, '        if len(rows) > REORDER_MAX:\n',
     '        if False:\n', 'test_more_than_64_pending_is_too_long_and_rejected_by_validation'),
    ('deshacer exige la fila exacta que dejó el recibo', C, "                    and e['queue_etag'] == etag == last['after_etag']):",
     '                    ):', 'test_undo_conflicts_have_no_effects'),
    ('deshacer dentro de su ventana', C, "self.clock() - last['applied_at'] <= REORDER_UNDO_SECONDS",
     'True', 'test_undo_conflicts_have_no_effects'),
    ('deshacer sólo el último recibo del grupo', C, 'done and done[-1] is prior',
     'True', 'test_undo_conflicts_have_no_effects'),
    ('un add posterior invalida deshacer', C, "undo=sorted(t['id'] for t in rows) == sorted(r['plan'])",
     'undo=True', 'test_add_after_comparison_keeps_its_id_stays_out_and_invalidates_undo'),
    ('mata sólo por PID e instante de arranque', C, 'return start is not None and spawned - 2 <= start <= spawned + 2',
     'return start is not None', 'test_reused_pid_is_not_killed'),
    ('PID del editor durable antes de abrir la puerta', C, "editing=dict(id=task['id'], pid=process.pid, spawned=self.wall())",
     'editing=None', 'test_orphan_editor_is_killed_only_by_pid_and_start_and_never_edited_twice'),
    ('espera o mata al huérfano antes de reparar', C, "        if r['editing']:\n            self._reorder_reap(r['editing'])",
     '        if False:\n            pass', 'test_orphan_editor_is_killed_only_by_pid_and_start_and_never_edited_twice'),
    ('Stashed propio sólo tras reinicio', C, "or (name == 'Stashed' and restarted and detail.get('enqueue_at') is None)",
     "or (name == 'Stashed' and detail.get('enqueue_at') is None)", 'test_stashed_without_restart_is_not_ours'),
    ('nunca enqueue con log previo', C, "if not own or os.path.lexists(self.reorder_paths()[0] / 'task_logs' / (ident + '.log')):",
     'if not own:', 'test_locked_with_log_is_never_enqueued'),
    ('enqueue de recuperación se persiste', C, "if r.get('enqueued_again') and paused and not started:",
     'if False:', 'test_enqueue_recovery_is_persisted_when_the_group_stays_closed'),
    ('reabrir sólo con admisión fresca: presión', C, "elif decide('mini', pressure_mini(), threshold) == 'pausar':",
     'elif False:', 'test_new_pressure_keeps_order_and_hands_pause_to_guard'),
    ('reabrir sólo con admisión fresca: mantenimiento', C, "            if not ready:\n                reasons.add('ambiguous')",
     "            if False:\n                reasons.add('ambiguous')", 'test_accepted_action_survives_actions_disabled_and_maintenance'),
    ('ajeno en la fila retiene sin revertir', C, "        if foreign:\n            return self._reorder_hold(key)\n        r = self._reorder_phase(key, phase='rolling_back')",
     "        r = self._reorder_phase(key, phase='rolling_back')", 'test_foreign_edit_during_action_holds_group_without_success'),
    ('guardia es el recuperador', C, '                self.recover_reorder()\n            except errors as error:',
     '                pass\n            except errors as error:', 'test_crash_at_each_frontier_is_settled_by_the_single_recoverer'),
    ('apagar acciones no abandona una aceptada', C, "        if self.host != 'mini':\n            return\n        with self.connect() as db:\n            bodies",
     "        if self.host != 'mini' or self.disabled():\n            return\n        with self.connect() as db:\n            bodies", 'test_guard_recovers_even_when_actions_are_disabled'),
    ('mismo id devuelve el mismo recibo', C, "        if row:\n            return (self.reply(request, 'conflict', 'idempotency_conflict') if row[0] != digest\n                    else json.loads(row[1])['response'])\n        if not request['dry_run']:",
     "        if not request['dry_run']:", 'test_same_id_returns_same_receipt_and_new_body_conflicts'),
    ('etag cubre enqueued_at', C, "t['priority'], task_state(t)[1]['enqueued_at']]",
     "t['priority']]", 'test_view_serves_opaque_tokens_and_etag_over_whole_row'),
    ('el desk no reordena', C, "        if self.host != 'mini':\n            return self.reply(request, 'unsupported', 'unsupported', effects=False)\n        key, op, e",
     '        key, op, e', 'test_desk_stays_without_reorder'),
    ('preparador sólo restaura Locked propio', C, "elif (variant == 'Locked' and owned.get(ident) == task_identity(task)",
     "elif (variant == 'Locked' and owned.get(ident) is not None", 'test_preparer_restores_own_locked_and_holds_foreign'),
    ('editor: huella de identidad', E, "    if fingerprint(task['command'], task['path'], task['label']) != plan['fingerprint']:",
     '    if False:', 'test_rewrite_checks_plan_and_identity'),
    ('editor: prioridad previa del plan', E, "task['priority'] != plan['before']",
     'False', 'test_rewrite_checks_plan_and_identity'),
    ('editor: huella incluye la etiqueta', E, 'body = json.dumps([command, path, label],',
     'body = json.dumps([command, path],', 'test_null_label_is_absent_and_fingerprinted_as_none'),
    ('editor: claves en orden y sin repetir', E, ' or order != [k for k in KEYS if k in fields]:',
     ':', 'test_unrecognized_formats_are_rejected'),
    ('editor: priority inequívoca, no por regex', E, "    start, end = task['span']\n",
     "    start, end = re.search(r'^priority = (-?[0-9]+)$', text, re.M).span(1)\n", 'test_multiline_command_with_priority_line_is_not_the_key'),
    ('editor: plan privado 0600', E, 'or not private(path, 0o600)',
     '', 'test_main_requires_private_plan_and_own_tasks_file'),
    ('editor: TOML dentro del directorio privado', E, 'root not in target.resolve().parents or ',
     '', 'test_main_requires_private_plan_and_own_tasks_file'),
    ('editor: sin enlaces simbólicos', E, ' or target.is_symlink():',
     ':', 'test_main_requires_private_plan_and_own_tasks_file'),
    # Instalación: el ayudante viaja con la biblioteca y una intención previa a U3.6 sigue valiendo.
    ('instalación publica el ayudante', 'mini.py', "MODULES = ['mini.py', 'control.py', control.EDITOR_NAME]",
     "MODULES = ['mini.py', 'control.py']", 'test_install_publishes_reorder_helper_without_restart', 'test_mini_table.py'),
    ('intención previa al ayudante se acepta', 'mini.py', "set(intent['files']) not in (names, names - {control.EDITOR_NAME})",
     "set(intent['files']) != names", 'test_intent_from_before_the_helper_still_recovers', 'test_mini_table.py'),
]


def main():
    killed = 0
    for rule, relative, original, replacement, test, *pattern in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix='u36-opus-mutacion-', dir='/tmp') as temporary:
            copy = Path(temporary) / 'pueue-desk'
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns('__pycache__'))
            target = copy / relative
            source = target.read_text()
            if source.count(original) != 1:
                raise ValueError(f'Mutación ambigua o caduca: {rule}')
            target.write_text(source.replace(original, replacement))
            command = [sys.executable, '-m', 'unittest', 'discover', '-s', str(copy / 'tests'),
                       '-p', (pattern or ['test_reorder.py'])[0], '-k', test, '-v']
            result = subprocess.run(command, text=True, capture_output=True, cwd=str(copy / 'tests'))
            output = result.stdout + result.stderr
            rejected = result.returncode != 0 and 'FAIL: ' + test in output and '\nERROR: ' not in output
            if not rejected:
                print(output, flush=True)
                raise AssertionError(f'Mutación sobrevivió o no falló por aserción: {rule}')
            killed += 1
            evidence = [line for line in output.splitlines() if line.startswith(('FAIL:', 'AssertionError:'))]
            print(f'RECHAZADA {killed:02}: {rule} | {test} | {"; ".join(evidence[:2])}', flush=True)
    print(f'Resultado: {killed}/{len(MUTATIONS)} mutaciones rechazadas por aserción ({sys.version.split()[0]})', flush=True)


if __name__ == '__main__':
    main()
