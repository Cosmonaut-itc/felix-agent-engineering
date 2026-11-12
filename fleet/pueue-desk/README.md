# Pueue en el desk

Servicio systemd **de usuario** para Pueue 4.0.4 en WSL, listo para instalar
tras U10 (NVMe). No instala binarios ni migra clientes de deskpolicy.

Desde la MacBook: `fleet/pueue-desk/deploy-desk.sh --seco` comprueba sin escribir;
`fleet/pueue-desk/deploy-desk.sh` instala por `ssh desk`. Requiere `pueue` y
`pueued` en `/home/felix/.local/bin`, systemd de usuario y permiso para
`loginctl enable-linger felix`. El instalador lo pide antes de detener un
`pueued` manual: si se rechaza, termina con error y el daemon manual sigue vivo.
Un fallo de `aplicar` no reinicia `pueued`: `pueue-desk-mode.service` reintenta
cada 30 s y deja el error en `journalctl --user -u pueue-desk-mode`.
Linger inicia el servicio al arrancar la distro; el arranque de Windows debe
iniciar WSL por su mecanismo habitual (SSH despierta la distro).

`control.py` contiene la única tabla de cuotas: hasito 3/4, jale 2/3,
otros 1/1 y pg5434 1/1 (día/noche). Noche: [05:00Z, 14:00Z).
La suma es 7/9 incluyendo pg5434; Pueue no impone un máximo global. Los timers
y el arranque aplican el modo UTC vigente, sin interrumpir tareas vivas.
Usar sólo esos grupos; `default` y otros grupos quedan fuera de estas cuotas.
El instalador elimina `suites` sólo si no tiene ninguna tarea.

Ver fila: `ssh desk '~/.local/bin/pueue status -g hasito'`.
Pausar a mano, dejando terminar tareas:
`ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py pausar hasito'`.
Reanudar: `ssh desk '~/.local/bin/pueue start -g hasito'`.
El guardia respeta grupos previamente pausados. **Si el guardia ya había
pausado el grupo, usar siempre el comando `pausar` anterior**: Pueue no distingue
una segunda pausa manual de la suya. Ese comando retira la propiedad del guardia
bajo el mismo candado; una pausa directa de Pueue no puede expresar ese relevo.

Cada 60 s el guardia lee PSI avg60 y MemAvailable. Pausa con E/S >40,
CPU >30 o RAM <4 GiB; reanuda sólo sus grupos con E/S <20, CPU <15 y RAM ≥6 GiB.
Los bordes intermedios mantienen el estado. Registro JSONL UTC, con lecturas,
decisiones y acciones: `ssh desk 'tail -f ~/.local/state/pao/pueue-desk/guardia.log'`.
Rota a `.1` al llegar a 1 MiB. Estado y candado comparten ese directorio.
Ante errores de lectura o de Pueue falla sin reanudar; consultar también
`journalctl --user -u pueue-desk-guard.service`.

Retirar: `fleet/pueue-desk/withdraw-desk.sh` (admite `--seco`). Detiene y
deshabilita timers y daemon y quita unidades; conserva cola, estado, biblioteca
y linger. No drenar tareas automáticamente: retirar cuando hayan terminado.

Pruebas: `/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v`.
ShellCheck: `shellcheck --severity=warning fleet/pueue-desk/*-desk.sh`.

## Guardia de la Mac Mini

La Mini comparte la lógica de `control.py` (perfil `--host mini`), pero lee la
carga de 1 minuto con `os.getloadavg()`, los núcleos con `os.cpu_count()` y la
presión de memoria con `/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level`.
Los umbrales viven en `PRESSURE_THRESHOLDS["mini"]`:

| Acción | Carga1 / núcleos | Presión de memoria |
| --- | --- | --- |
| Pausar admisión | > 2,0 **o** | ≥ 2 (aviso 2 o crítico 4) |
| Reanudar grupos propios | < 1,2 **y** | = 1 (normal) |

Así, con 10 núcleos se pausa por carga >20 y sólo se reanuda por carga <12
con memoria normal. Los bordes mantienen el estado. Una lectura ausente,
no finita, negativa o un nivel de memoria desconocido causa error, se registra
y no decide ni modifica grupos.

En la Mini, desde este checkout (no instala Pueue):

```sh
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar
```

`--seco` imprime el plan sin ejecutar comandos ni escribir; las comprobaciones
se ejecutan al instalar. Antes de cualquier cambio exige el LaunchAgent
`sh.brew.pueue` cargado y una respuesta válida de `/opt/homebrew/bin/pueue status
--json` con `suites` y `default`. No arranca, detiene ni reinicia ese daemon,
ni modifica `parallel`. La instalación es idempotente y copia el controlador a
`~/.local/lib/pao-pueue-mini/control.py`, renderiza rutas absolutas en
`~/Library/LaunchAgents/local.pao.pueue-mini.guard.plist` y usa
`launchctl bootstrap gui/$(id -u)`; para retirar usa `bootout` del servicio de
la guardia. El LaunchAgent ejecuta `/usr/bin/python3 -I` cada 60 s
(`StartInterval`, sin `KeepAlive`).

Afecta sólo a `suites` y `default`. No reclama grupos ya pausados, y suelta la
propiedad si observa que alguien los arrancó. Para tomar manualmente una pausa
que hizo la guardia, usar el mismo candado y retirar su propiedad:

```sh
/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/control.py pausar suites --host mini
/opt/homebrew/bin/pueue start -g suites
```

Una segunda pausa directa de Pueue no expresa ese relevo. El estado
`guardia-state.json`, `control.lock` y el log JSONL UTC viven en
`~/.local/state/pao/pueue-mini`. Verlo con
`tail -f ~/.local/state/pao/pueue-mini/guardia.log`; rota a `guardia.log.1` a
1 MiB. Los errores de ejecución de launchd aparecen en `launchd.err.log` del
mismo directorio. Desinstalar reanuda los grupos que pausó la guardia y vacía su propiedad; las
pausas manuales no son suyas y siguen en pausa. Conserva el log.

**Límite:** sólo frena la admisión: las tareas que ya corren siguen. Las
corridas de `remote-run` encolan en `suites`; las corridas de Jale en la Mini
no pasan por Pueue y esta guardia no las frena.

Pruebas en la Mini, siempre mediante su Pueue:

```sh
REMOTE_RUN_HOST=mini ~/.local/bin/remote-run start --name dirigida-u18 -- '/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v'
REMOTE_RUN_HOST=mini ~/.local/bin/remote-run wait ID
```

Mutaciones dirigidas (también dentro de `remote-run`):
`/usr/bin/python3 -I fleet/pueue-desk/tests/mutate_mini.py`.
