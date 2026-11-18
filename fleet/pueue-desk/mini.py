#!/usr/bin/python3 -I
"""Arranque fijo y relevo Mini. El llamador toma control.lock; no hay otro job."""
import argparse
import ast
import importlib.util
import json
import math
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('mini_control', ROOT / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
LABEL = 'local.pao.pueue-mini.daemon'
GUARD = 'local.pao.pueue-mini.guard'
BREW = 'sh.brew.pueue'
PUEUE = '/opt/homebrew/bin/pueue'
DAEMON = '/opt/homebrew/bin/pueued'
TERMINAL = ('done', 'withdrawn', 'aborted')
START_LOG_BYTES = 65536
# Only our fixed messages may reach diagnostics. Stock/subprocess exceptions
# can contain task commands, environments, config contents or secrets.
START_REASONS = {
    'Autorización de arranque cambió durante la recogida cliente',
    'Host en mantenimiento', 'El arranque debe pertenecer al MainPID de launchd',
    'Homebrew no está retirado; no se arranca otro daemon',
    'Daemon responde; preparar exige daemon detenido',
    'PID anterior vivo o ambiguo; no se prepara', 'PID ilegible; no se prepara',
    'PID configurado difiere de la autoridad', 'Cola cambió durante preparación; conservar archivos',
    'Intención de arranque inconsistente', 'Autoridad inválida', 'Deseo inconsistente',
    'Propiedad inválida', 'Versión o propiedad de arranque inválida', 'Arranque durable inválido',
    'Estado Pueue incompleto; arranque abortado sin reemplazar la cola',
    'Cola Pueue incompatible; arranque abortado sin sustituirla',
    'Relevo ilegible; conservar cierre y archivos', 'Se requiere Pueue estándar 4.0.4',
}
JSON_REASONS = {
    'Expecting value', 'Expecting property name enclosed in double quotes',
    'Unterminated string starting at', 'Invalid control character at',
    'Invalid \\escape', 'Invalid \\uXXXX escape', 'Expecting \',\' delimiter',
    'Expecting \':\' delimiter', 'Extra data',
}


def daemon_environment(home):
    return control.mini_environment(home)


def configuration_source(home, cwd):
    # Pueue 4.0.4 settings.rs:393: standard macOS config, then cwd/pueue.yml.
    for source in (home / 'Library/Application Support/pueue/pueue.yml', cwd / 'pueue.yml'):
        if source.is_file():
            return source.resolve()
    return None


def launchctl(*args, check=True):
    return subprocess.run(['/bin/launchctl', *args], check=check, text=True,
                          capture_output=True, timeout=8)


class Manager:
    def __init__(self, home=None, pueue=None, directory=None, launch=launchctl,
                 checkpoint=lambda phase: None, configuration=None):
        self.home = Path(home or Path.home())
        self.directory = Path(directory or self.home / '.local/state/pao/pueue-mini')
        self.library = self.home / '.local/lib/pao-pueue-mini'
        self.agents = self.home / 'Library/LaunchAgents'
        self.domain = 'gui/' + str(os.getuid())
        self.pueue = pueue or control.Pueue(PUEUE, env=daemon_environment(self.home), cwd=self.home)
        self.engine = control.MiniControl(self.pueue, self.directory)
        self.launch, self.checkpoint = launch, checkpoint
        self.configuration = configuration

    def state(self):
        with self.engine.connect() as db:
            return self.engine.state(db)

    def save(self, state, phase):
        with self.engine.connect(write=True) as db:
            self.engine.store(db, state)
        self.checkpoint(phase)

    def transaction(self, state):
        value = state.get('mini_handoff')
        if value is not None:
            control.validate_mini_handoff(value)
            if set(value['original_disabled']) != {BREW, LABEL, GUARD}:
                raise ValueError('Gestores de relevo desconocidos')
        return value

    def loaded(self, label):
        result = self.launch('print', self.domain + '/' + label, check=False)
        if result.returncode not in (0, 113):
            raise ValueError('No se pudo consultar launchd')
        return result.stdout if result.returncode == 0 else None

    def disabled(self, label):
        output = self.launch('print-disabled', self.domain).stdout
        if not output.strip().startswith((self.domain + ' => {', 'disabled services = {')) or not output.strip().endswith('}'):
            raise ValueError('Estado disable de launchd ilegible')
        entries = {}
        for line in output.strip().splitlines()[1:-1]:
            if not line.strip():
                continue
            match = re.fullmatch(r'\s*"([^"\n]+)"\s*=>\s*(true|false|enabled|disabled)\s*,?\s*', line)
            if not match or match[1] in entries:
                raise ValueError('Entrada disable de launchd ilegible')
            entries[match[1]] = match[2] in ('true', 'disabled')
        return entries.get(label, False)

    def effect(self, state, name, operation):
        # Persist before EVERY external effect; replay is intentional and idempotent.
        state['mini_handoff']['effect'] = name
        self.save(state, 'intent:' + name)
        operation()
        self.checkpoint('effect:' + name)
        state['mini_handoff']['effect'] = None
        self.save(state, 'complete:' + name)

    def command(self, state, verb, label):
        self.effect(state, verb + ':' + label,
                    lambda: self.launch(verb, self.domain + '/' + label))

    def bootout(self, state, label):
        def unload():
            result = self.launch('bootout', self.domain + '/' + label, check=False)
            if result.returncode not in (0, 3, 113):
                result.check_returncode()
            deadline = time.monotonic() + 5
            while self.loaded(label) is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('launchd no descargó el servicio: ' + label)
                time.sleep(min(.1, remaining))
        self.effect(state, 'bootout:' + label, unload)

    def bootstrap(self, state, label):
        if self.loaded(label) is None:
            self.effect(state, 'bootstrap:' + label,
                        lambda: self.launch('bootstrap', self.domain, str(self.agents / (label + '.plist'))))

    def configuration_for_host(self):
        # Production always shares this validator with preparar; explicit paths only in private tests.
        return self.configuration or control.validate_configuration(home=self.home, env={}, cwd=self.home,
                                                                    pueue=PUEUE, daemon=DAEMON)

    def preflight(self, target):
        config = self.configuration_for_host()
        queue = control.read_queue(config['data'] / 'state.json')
        control.start_projection(self.engine, config, allow_pending=False, policy_migration=target == 'install')
        with self.engine.connect() as db:
            state = self.engine.state(db, policy_migration=target == 'install')
        self.transaction(state)
        if Path(state['pid_file']).resolve() != config['pid_file'].resolve():
            raise ValueError('PID configurado distinto de la autoridad')
        with self.engine.connect() as db:
            if any(json.loads(row[0])['response']['status'] in ('applying', 'partial', 'unknown')
                   for row in db.execute('SELECT body FROM host_actions')):
                raise ValueError('Hay recibos pendientes; no se releva')
        previous = self.transaction(state)
        if previous and previous['phase'] not in TERMINAL:
            raise ValueError('Relevo pendiente; lo atiende la guardia')
        if self.loaded(GUARD) is None or self.disabled(GUARD):
            raise ValueError('La guardia existente debe estar cargada y habilitada antes del relevo')
        if set(self.engine.profile) != set(control.MINI_GROUPS) or control.MINI_GROUPS != ('suites', 'default', 'rapidas'):
            raise ValueError('Perfil Mini incompatible')
        control.validate_target(self.engine.profile, self.engine.profile, cap=self.engine.cap(state))
        for g in control.MINI_GROUPS:
            if g not in queue['groups']:
                raise ValueError('Falta un grupo Mini en la cola; no readoptar')
        source = LABEL if self.loaded(LABEL) is not None else BREW
        if self.loaded(source) is None:
            raise ValueError('No hay un daemon gestionado que relevar')
        if self.loaded(LABEL) is not None and self.loaded(BREW) is not None:
            raise ValueError('Dos gestores cargados; no se releva')
        # Check the exact PID owner, pinned file, identity/date, desired policy and live vector.
        identity = self.verify_owner(source, state)
        repair = (target == 'install' and source == LABEL and previous and previous['phase'] == 'done'
                  and not self.table_installed())
        if state['policy_hash'] == self.engine.policy_hash and not repair:
            self.engine.view()
        elif not (target == 'install' and previous and previous['phase'] == 'done' and source == LABEL
                  and self.disabled(BREW)
                  and identity['generation'] == state['generation']
                  and state.get('startup', {}).get('phase') in (None, 'ready')):
            raise ValueError('Migración de tabla requiere relevo done y generación vigente')
        else:
            self.engine.observed()
        brew = plistlib.loads((self.agents / (BREW + '.plist')).read_bytes())
        argv = brew.get('ProgramArguments', [])
        brew_cwd = Path(brew.get('WorkingDirectory', str(self.home)))
        if (brew.get('Label') != BREW or not isinstance(argv, list) or len(argv) not in (1, 2)
                or argv[1:] not in ([], ['--verbose']) or not Path(argv[0]).is_absolute()
                or Path(argv[0]).resolve() != Path(DAEMON).resolve()
                or brew_cwd not in (self.home, Path('/opt/homebrew/var'))
                or 'EnvironmentVariables' in brew):
            raise ValueError('LaunchAgent Homebrew incompatible; conservarlo')
        if configuration_source(self.home, brew_cwd) != configuration_source(self.home, self.home):
            raise ValueError('Homebrew y destino seleccionan configuración distinta; conservar origen')
        return state, config, {label: self.disabled(label) for label in (BREW, LABEL, GUARD)}, identity

    def verify_owner(self, label, state):
        info = self.loaded(label)
        pid = Path(state['pid_file']).read_text().strip()
        if info is None or not pid.isdigit() or not re.search(r'^\s*pid = ' + re.escape(pid) + r'\s*$', info, re.M):
            raise ValueError('PID no pertenece al LaunchAgent esperado')
        generation = self.engine.generation(state)  # pid file date + ps lstart/comm, never /proc
        return dict(owner='prepared' if label == LABEL else 'homebrew', pid=int(pid), generation=generation)

    def source(self, state):
        value = self.transaction(state)
        if 'source' not in value:
            # Legacy intentions have no trustworthy origin. Only a live, unique
            # launchd/PID identity can upgrade them; target is never evidence.
            owners = [label for label in (LABEL, BREW) if self.loaded(label) is not None]
            if len(owners) != 1:
                raise ValueError('Origen del relevo no verificable; conservar cierre')
            value['source'] = self.verify_owner(owners[0], state)
            value['version'] = 2
            if value['phase'] == 'aborted':
                value['retained_owner'] = value['source']['owner']
            self.save(state, 'handoff:source')
        return value['source']

    def publish(self, target, data):
        name = str(target.relative_to(self.home))
        self.checkpoint('publish:before:' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.u35-tmp')
        with temporary.open('wb') as stream:
            stream.write(data)
            control.sync_file(stream)
        self.checkpoint('publish:synced:' + name)
        os.replace(temporary, target)
        self.checkpoint('publish:renamed:' + name)
        control.sync_directory(target.parent)
        self.checkpoint('publish:after:' + name)

    def render(self, label, template=None):
        value = plistlib.loads(template if template is not None else
                              (ROOT / 'launchagents' / (label + '.plist')).read_bytes())
        for key, item in list(value.items()):
            if type(item) is str:
                value[key] = item.replace('@HOME@', str(self.home))
            elif type(item) is list:
                value[key] = [v.replace('@HOME@', str(self.home)) for v in item]
        return plistlib.dumps(value)

    def install_recoverer(self):
        # No daemon/plist candidate is changed before the recoverer is available.
        # The existing guard executes control.py afresh every tick, so no bootout gap.
        # Templates precede the modules: every accepted intention can be repaired
        # entirely from the installed library, even when the checkout disappears.
        names = ['launchagents/' + label + '.plist' for label in (LABEL, GUARD)]
        for name in names + ['mini.py', 'control.py']:
            self.publish(self.library / name, (ROOT / name).read_bytes())
        self.publish(self.agents / (GUARD + '.plist'), self.render(GUARD))
        if self.disabled(GUARD):
            raise ValueError('Guardia deshabilitada; resolver antes del relevo')
        if self.loaded(GUARD) is None:
            raise ValueError('No hay recuperador cargado; no se releva')

    def table_plan(self, state):
        """Installer only, under control.lock; receipts and pause ownership stay intact."""
        if state['policy_hash'] == self.engine.policy_hash:
            return None
        desired = dict(self.engine.profile)
        desired.update({g: item['slots'] for g, item in state['overrides'].items()})
        overrides = state['overrides']
        if sum(desired.values()) > self.engine.cap(state):
            # As on desk, retire active adjustments together, never delete their receipts.
            desired, overrides = dict(self.engine.profile), {}
        return dict(state, policy_hash=self.engine.policy_hash, desired=desired,
                    overrides=overrides, revision=state['revision'] + 1)

    def table_files(self):
        names = ['launchagents/' + label + '.plist' for label in (LABEL, GUARD)] + ['mini.py', 'control.py']
        return {name: (ROOT / name).read_text() for name in names}

    def table_installed(self):
        return all((self.library / name).is_file() and (self.library / name).read_bytes() == data.encode()
                   for name, data in self.table_files().items())

    def recover_table(self):
        """Roll forward only an accredited intent; caller holds control.lock.

        The complete bundle precedes its first publication. Recovery needs neither
        the checkout nor a live daemon, including a wrapper importing old control.py.
        Ordinary authority reads remain strict. Receipts are never rewritten.
        """
        journal = self.directory / 'table-migration.json'
        if not journal.exists():
            return None
        intent = json.loads(journal.read_text())
        names = {'launchagents/' + label + '.plist' for label in (LABEL, GUARD)} | {'mini.py', 'control.py'}
        if (set(intent) != {'version', 'source_hash', 'target_hash', 'files'} or intent['version'] != 1
                or any(type(intent[k]) is not str or not re.fullmatch('[0-9a-f]{64}', intent[k])
                       for k in ('source_hash', 'target_hash'))
                or type(intent['files']) is not dict or set(intent['files']) != names
                or any(type(data) is not str for data in intent['files'].values())):
            raise ValueError('Intención de tabla inválida')
        # Validate the literal profile before publishing anything from the bundle.
        profiles = [node.value for node in ast.parse(intent['files']['control.py']).body
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'MINI_PROFILE'
                                                           for t in node.targets)]
        if len(profiles) != 1:
            raise ValueError('Perfil de la intención inválido')
        profile = ast.literal_eval(profiles[0])
        control.validate_target(profile, profile, cap=7)
        digest = control.hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        if digest != intent['target_hash']:
            raise ValueError('Hash de la intención de tabla inválido')
        config = self.configuration_for_host()
        with self.engine.connect() as db:
            state = self.engine.state(db, policy_migration=True)
            pending = any(json.loads(row[0])['response']['status'] in ('applying', 'partial', 'unknown')
                          for row in db.execute('SELECT body FROM host_actions'))
        previous = self.transaction(state)
        if state['policy_hash'] not in (intent['source_hash'], intent['target_hash']) or pending or not previous:
            raise ValueError('Intención de tabla no acredita esta autoridad o dueño')
        if (previous['target'] == 'install' and previous.get('maintenance_owned')
                and previous['phase'] in ('maintenance', 'switch', 'verify')):
            # Let the existing handoff reconstruct its owner first. No old hash
            # is accepted by that path: its installed runtime must still agree.
            self.state()
            return None
        control.start_projection(self.engine, config, allow_pending=False, policy_migration=True)
        if (previous['phase'] != 'done'
                or self.loaded(BREW) is not None or not self.disabled(BREW)
                or self.loaded(LABEL) is None):
            raise ValueError('Intención de tabla no acredita esta autoridad o dueño')
        for name, data in intent['files'].items():
            self.publish(self.library / name, data.encode())
        # Load the completed bundle, never a partially published module pair.
        spec = importlib.util.spec_from_file_location('mini_table_recovered', self.library / 'control.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        engine = runtime.MiniControl(self.pueue, self.directory,
                                     generation=self.engine.generation_source, clock=self.engine.clock)
        if engine.policy_hash != intent['target_hash']:
            raise ValueError('Biblioteca no corresponde a la intención de tabla')
        control.validate_target(engine.profile, engine.profile, cap=engine.cap(state))
        # Rebase on the current authority: even an older installed guard may have
        # recorded a generation or a manual pause after the installer died.
        original = self.engine
        try:
            self.engine = engine
            migration = self.table_plan(state)
        finally:
            self.engine = original
        if migration is not None:
            self.save(migration, 'table:migrated')
        with engine.connect() as db:
            engine.state(db)  # strict target validation before forgetting the intent
        template = intent['files']['launchagents/' + GUARD + '.plist'].encode()
        self.publish(self.agents / (GUARD + '.plist'), self.render(GUARD, template))
        self.checkpoint('table:validated')
        journal.unlink()
        control.sync_directory(journal.parent)
        self.checkpoint('table:complete')
        return engine

    def update_table(self, state, dry):
        migration = self.table_plan(state)
        if dry:
            if migration is not None:
                self.announce_table(state, migration, 'SECO: ')
            return state
        if migration is None and self.table_installed():
            return state
        intent = dict(version=1, source_hash=state['policy_hash'], target_hash=self.engine.policy_hash,
                      files=self.table_files())
        self.publish(self.directory / 'table-migration.json', json.dumps(intent).encode())
        self.checkpoint('table:intent')
        self.recover_table()
        # Only quotas: reconciliation never starts groups or restarts the daemon.
        if not self.engine.reconcile():
            raise ValueError('Tabla migrada; cuotas pendientes de reconciliar por la guardia')
        self.checkpoint('table:reconciled')
        if migration is not None:
            self.announce_table(state, migration)
        return self.state()

    def announce_table(self, state, migration, prefix=''):
        adjustments = 'retirar ajustes' if state['overrides'] and not migration['overrides'] else 'conservar ajustes'
        print(prefix + 'migración de tabla: ' + adjustments + '; revisión ' + str(migration['revision'])
              + '; policy_hash ' + migration['policy_hash']
              + '; desired ' + json.dumps(migration['desired'], sort_keys=True))

    def change(self, target, dry=False, wait=0):
        if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 7200:
            raise ValueError('Espera inválida; usar entre 0 y 7200 segundos')
        # Even the dry branch takes the EXISTING lock read-only; never creates directories.
        with control.locked(self.directory, create=False):
            if target == 'install' and not dry:
                self.recover_table()
            state, config, original, source = self.preflight(target)
            previous = self.transaction(state)
            same_owner = source['owner'] == ('prepared' if target == 'install' else 'homebrew')
            if same_owner and previous:
                if target == 'install' and previous['phase'] == 'done':
                    state = self.update_table(state, dry)
                previous = self.transaction(state)
                if dry:
                    print('SECO: destino ya es el gestor vivo verificado; sin relevo ni drenado')
                    return
                if os.path.lexists(self.directory / 'maintenance'):
                    raise ValueError('Mantenimiento; no se inicia el relevo')
                # No publication, launchd effects, pauses or drain: even a Running
                # task is untouched. Persist the observed stable owner atomically.
                phase = 'done' if target == 'install' else 'withdrawn'
                if previous['phase'] != phase:
                    state['mini_handoff'] = dict(version=2, target=target, phase=phase, effect=None,
                        original_disabled=previous['original_disabled'], deadline=None,
                        source=source, retained_owner=source['owner'])
                    self.save(state, 'handoff:converged')
                return
            if not wait and self.active(self.pueue.status()):
                raise ValueError('Hay tareas en curso; indicar --esperar para drenar sin matarlas')
            if dry:
                print('SECO: validar configuración/cola/autoridad/PID y recibos sin efectos')
                print('SECO: publicar recuperador existente; migrar aditivamente bajo control.lock')
                print('SECO: intención durable; deshabilitar destino antes de publicar su plist; cerrar y drenar')
                print('SECO: disable origen → bootout → verificar PID muerto → preparar → enable/bootstrap destino')
                print('SECO: verificar dueño/cuotas; sensores frescos; retirar sólo pausas propias')
                print('SECO: plazo de drenado ' + str(wait) + ' s; al vencer conservar el daemon origen')
                return
            if os.path.lexists(self.directory / 'maintenance'):
                raise ValueError('Mantenimiento; no se inicia el relevo')
            if target == 'withdraw' and not state.get('mini_handoff'):
                raise ValueError('No hay instalación U3.5 que retirar')
            self.install_recoverer()
            # Reinstallation keeps the original Homebrew enable/disable state.
            if previous:
                original = previous['original_disabled']
            state['startup_version'] = 1
            state['mini_handoff'] = dict(version=2, target=target, phase='stage', effect=None, source=source,
                                        original_disabled=original, deadline=self.engine.clock() + wait if wait else None)
            self.save(state, 'handoff:intent')
            self._recover(config)

    @staticmethod
    def active(snapshot):
        return any(set(t['status']) & {'Running', 'Paused'} for t in snapshot['tasks'].values())

    def abort(self):
        state = self.state()
        value = self.transaction(state)
        source = self.source(state)
        for group, reasons in state['reasons'].items():
            if 'handoff' in reasons:
                state['reasons'][group] = sorted((set(reasons) - {'handoff'}) | {self.release_reason(group)})
        state.pop('stop_tasks', None)
        # Abort is only reached before switch: the source is still the owner.
        value.update(phase='aborted', effect=None,
                     retained_owner=source['owner'])
        self.save(state, 'handoff:aborted')
        # No start here: the existing guard reconciles and samples fresh sensors.
        return True

    def release_reason(self, group):
        # The guard samples only its quota profile. Foreign groups must remain
        # explicitly resolvable by control.py reanudar, not owned by that guard.
        return 'pressure' if group in self.engine.profile else 'ambiguous'

    def _recover(self, config=None):
        """Caller holds control.lock. False means sensors/admission must wait."""
        recovered = self.recover_table()
        if recovered is not None and recovered.policy_hash != self.engine.policy_hash:
            return False  # next guard tick imports the completed installed bundle
        state = self.state()
        value = self.transaction(state)
        if os.path.lexists(self.directory / 'maintenance') or value and value['phase'] == 'maintenance':
            return False
        if value and value['phase'] == 'aborted' and value['version'] == 1:
            self.source(state)
        if value and value['phase'] == 'aborted' and 'retained_owner' not in value:
            value['retained_owner'] = self.source(state)['owner']
            self.save(state, 'handoff:retained-owner')
        if value is None or value['phase'] in TERMINAL:
            return True
        config = config or self.configuration_for_host()
        control.read_queue(config['data'] / 'state.json')  # never replace dubious files
        target = LABEL if value['target'] == 'install' else BREW
        other = BREW if target == LABEL else LABEL
        if value['phase'] == 'stage':
            self.command(state, 'disable', target)
            if target == LABEL:
                self.effect(state, 'publish:' + LABEL,
                            lambda: self.publish(self.agents / (LABEL + '.plist'), self.render(LABEL)))
            value['phase'] = 'drain'
            self.save(state, 'handoff:drain')
        if value['phase'] == 'drain':
            if value['deadline'] is not None and self.engine.clock() >= value['deadline']:
                return self.abort()
            # Idempotent pause ownership precedes each RPC, including the pause→reply gap.
            try:
                snapshot = self.pueue.status()
            except subprocess.CalledProcessError:
                control.assert_offline(self.pueue, config['pid_file'])
                value['phase'] = 'switch'
                self.save(state, 'handoff:source-exited')
                return False
            for group, row in snapshot['groups'].items():
                reasons = set(state['reasons'].get(group, []))
                if row['status'] == 'Running' or 'handoff' in reasons:
                    reasons.add('handoff')
                    state['reasons'][group] = sorted(reasons)
                    self.save(state, 'pause:intent:' + group)
                    self.pueue.call('pause', '--wait', '-g', group)
                    self.checkpoint('pause:effect:' + group)
            self.engine.record_stop()
            if self.active(self.pueue.status()):
                if value['deadline'] is None:
                    return self.abort()
                return False
            # The final read proved all work terminal BEFORE bootout. The older
            # record cannot attribute a cancellation during draining to this stop.
            state = self.state(); value = self.transaction(state)
            state.pop('stop_tasks', None)
            value['phase'] = 'switch'
            self.save(state, 'handoff:switch')
        if value['phase'] == 'switch':
            # Mandatory even if bootout already happened: login reconstructs from enabled plists.
            self.command(state, 'disable', other)
            self.bootout(state, other)
            if self.loaded(target) is not None:
                # An aborted opposite handoff may have kept this daemon alive.
                # stage disabled it; restore its override without restarting it.
                self.command(state, 'enable', target)
                value['phase'] = 'verify'
                self.save(state, 'handoff:verify')
                return False
            try:
                control.assert_offline(self.pueue, config['pid_file'])
            except ValueError:
                return False  # bootout may return before its verified PID/socket has exited
            if target == BREW:
                # Brew lacks a pre-start hook: prepare this one return, before enabling it.
                control.prepare_start(self.engine, configuration=config)
                state = self.state(); value = self.transaction(state)
            self.command(state, 'enable', target)
            self.bootstrap(state, target)
            value['phase'] = 'verify'
            self.save(state, 'handoff:verify')
            return False  # the fixed start needs control.lock; release it first
        if value['phase'] == 'verify':
            # bootout can finish after switch observed a still-loaded target.
            # Replay switch's offline check and bootstrap on the next tick.
            if self.loaded(target) is None:
                value['phase'] = 'switch'
                self.save(state, 'handoff:retry-switch')
                return False
            # No kickstart/reset: KeepAlive owns retry; never a second daemon.
            if self.loaded(other) is not None or not self.disabled(other):
                raise ValueError('Origen todavía habilitado o cargado')
            self.verify_owner(target, state)
            if not self.engine.reconcile():
                return False
            state = self.state(); value = self.transaction(state)
            for group, reasons in state['reasons'].items():
                if 'handoff' in reasons:
                    state['reasons'][group] = sorted((set(reasons) - {'handoff'}) | {self.release_reason(group)})
            if target == BREW and value['original_disabled'][BREW]:
                self.command(state, 'disable', BREW)
            value['phase'] = 'done' if target == LABEL else 'withdrawn'
            value['maintenance_owned'] = False
            self.save(state, 'handoff:complete')
            # Authority, guard and both disabled plists survive withdrawal. No orphaned pause.
            return True
        return False

    def maintenance(self, resume=False):
        """Operator entry: marker → guard → daemon; inverse publishes a closed start."""
        with control.locked(self.directory, create=False):
            state = self.state()
            value = self.transaction(state)
            if value is None:
                raise ValueError('Mantenimiento U3.5 requiere el relevo instalado')
            marker = self.directory / 'maintenance'
            signature = b'pao-mini-maintenance@1\n'
            if resume:
                present = os.path.lexists(marker)
                if not present and value['phase'] in TERMINAL:
                    return
                if (not value.get('maintenance_owned') or value['phase'] not in ('maintenance', 'switch', 'verify')
                        or present and (marker.is_symlink() or not marker.is_file() or marker.read_bytes() != signature)):
                    raise ValueError('No se retira un mantenimiento ajeno; conservar el marcador')
                if present or value['phase'] == 'maintenance':
                    for label in (BREW, LABEL):
                        self.command(state, 'disable', label)
                        self.bootout(state, label)
                    value['phase'] = 'switch'
                    self.save(state, 'maintenance:restart-intent')
                self.command(state, 'enable', GUARD)
                self.bootstrap(state, GUARD)
                if present:
                    def remove_marker():
                        marker.unlink()
                        control.sync_directory(marker.parent)
                    self.effect(state, 'maintenance:remove', remove_marker)
                self._recover()
            else:
                # Publication before any stop; a killed caller cannot leave a reopening job.
                if value['phase'] == 'aborted':
                    owner = self.source(state)['owner']
                    value['target'] = 'install' if owner == 'prepared' else 'withdraw'
                    value.pop('retained_owner', None)
                value['phase'] = 'maintenance'
                value['maintenance_owned'] = value.get('maintenance_owned', False) or not os.path.lexists(marker)
                self.save(state, 'maintenance:intent')
                if not os.path.lexists(marker):
                    self.effect(state, 'maintenance:create', lambda: self.publish(marker, signature))
                self.command(state, 'disable', GUARD)
                self.bootout(state, GUARD)
                try:
                    self.engine.record_stop()
                except subprocess.CalledProcessError:
                    control.assert_offline(self.pueue, self.configuration_for_host()['pid_file'])
                state = self.state()
                for label in (BREW, LABEL):
                    self.command(state, 'disable', label)
                    self.bootout(state, label)

    def start(self, execute=None):
        try:
            return self._start(execute)
        except Exception as error:
            try:
                self.log_start_rejection(error)
            except OSError:
                pass  # Disk/permission failures must still reject the start.
            raise

    def log_start_rejection(self, error):
        message = str(error)
        reason = (message if message in START_REASONS else
                  'Cola JSON ilegible' if isinstance(error, json.JSONDecodeError) else
                  'Archivo inaccesible' if isinstance(error, OSError) else
                  'Preparación o autorización rechazada; revisar cola, PID y autoridad')
        record = dict(utc=control.datetime.now(control.timezone.utc).isoformat(),
                      evento='arranque_rechazado', fase=getattr(self, '_start_stage', 'autorización'), motivo=reason)
        if isinstance(error, json.JSONDecodeError) and error.msg in JSON_REASONS:
            record['detalle_json'] = error.msg  # fixed parser vocabulary, never its input document
        line = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        # Diagnostics are disposable, unlike authority. Rotation precedes append
        # and bounds BOTH files, even across repeated KeepAlive failures.
        with control.locked(self.directory, create=False):
            target = self.directory / 'arranque.log'
            if target.exists() and target.stat().st_size + len(line) > START_LOG_BYTES:
                target.replace(self.directory / 'arranque.log.1')
            descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, 'ab') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(line)

    def _start(self, execute=None):
        self._start_stage = 'autorización'
        if os.path.lexists(self.directory / 'maintenance'):
            raise ValueError('Host en mantenimiento')
        # Only launchd's unique MainPID may execute this wrapper. No inherited lock
        # can escape into tasks and block a restart while a stock child survives.
        info = self.loaded(LABEL)
        if info is None or not re.search(r'^\s*pid = ' + str(os.getpid()) + r'\s*$', info, re.M):
            raise ValueError('El arranque debe pertenecer al MainPID de launchd')
        with control.locked(self.directory, create=False):
            recovered = self.recover_table()
            if recovered is not None:
                self.engine = recovered
            state = self.state()
            value = self.transaction(state)
            authorized = value and ((value['target'] == 'install' and value['phase'] in ('switch', 'verify', 'done'))
                                    or (value['phase'] == 'aborted' and value.get('source', {}).get('owner') == 'prepared'
                                        and value.get('retained_owner') == 'prepared'))
            if (not authorized
                    or self.loaded(BREW) is not None or not self.disabled(BREW)):
                raise ValueError('Homebrew no está retirado; no se arranca otro daemon')
            self._start_stage = 'configuración'
            config = self.configuration_for_host()
            self._start_stage = 'preparación'
            control.prepare_start(self.engine, configuration=config)
            # CC2 collects outside control.lock. Do not execute a launch whose
            # handoff authorization changed while that window was open.
            if self.transaction(self.state()) != value:
                raise ValueError('Autorización de arranque cambió durante la recogida cliente')
            self.checkpoint('start:prepared')
            self._start_stage = 'exec'
            # control.lock is close-on-exec; launchd owns the daemon, stock owns tasks.
            if execute is None:
                os.execve(DAEMON, [DAEMON], daemon_environment(self.home))
            else:
                execute(DAEMON, [DAEMON])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('arrancar', 'recuperar', 'mantenimiento', 'continuar'))
    args = parser.parse_args()
    manager = Manager()
    if args.command == 'arrancar':
        manager.start()
    elif args.command in ('mantenimiento', 'continuar'):
        manager.maintenance(resume=args.command == 'continuar')
    else:
        with control.locked(manager.directory, create=False):
            manager.engine.reconcile_clients()
            if manager._recover():
                control.MiniControl(manager.pueue, manager.directory).guard(None, _collected=True)


if __name__ == '__main__':
    main()
